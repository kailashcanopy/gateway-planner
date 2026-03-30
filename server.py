import cv2
import numpy as np
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
import anthropic
import base64
import json
import os

app = Flask(__name__)
CORS(app)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

def detect_rooms(img_bytes):
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image")
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, blockSize=11, C=3)
    kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_close, iterations=3)
    kernel_dilate = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    walls = cv2.dilate(closed, kernel_dilate, iterations=1)
    floor = cv2.bitwise_not(walls)
    flood_mask = np.zeros((h + 2, w + 2), np.uint8)
    floor_copy = floor.copy()
    for pt in [(0,0),(w-1,0),(0,h-1),(w-1,h-1),(w//2,0),(w//2,h-1),(0,h//2),(w-1,h//2)]:
        if floor_copy[pt[1], pt[0]] == 255:
            cv2.floodFill(floor_copy, flood_mask, pt, 0)
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(floor_copy, connectivity=4)
    total_px = h * w
    rooms = []
    for i in range(1, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        rw   = stats[i, cv2.CC_STAT_WIDTH]
        rh   = stats[i, cv2.CC_STAT_HEIGHT]
        cx, cy = centroids[i]
        if area < total_px * 0.00006 or area > total_px * 0.08:
            continue
        aspect = max(rw, rh) / max(min(rw, rh), 1)
        if aspect > 6:
            continue
        rooms.append({"rx": float(cx/w), "ry": float(cy/h), "area": int(area), "aspect": float(aspect)})
    return rooms

def classify_facility(rooms):
    """
    Classify as large hospital vs outpatient/MOB based on room count and size.
    Large hospital: many small rooms (patient rooms ~100-300 sqft)
    MOB/outpatient: fewer, larger rooms (exam rooms ~150-400 sqft, offices bigger)
    """
    if len(rooms) > 80:
        return 'hospital'
    areas = sorted([r["area"] for r in rooms])
    median = areas[len(areas)//2] if areas else 0
    # If median room is large, likely MOB/outpatient
    return 'mob' if median > 3000 else 'hospital'

def select_gateways(rooms):
    """
    Gateway placement following deployment guidelines:
    1. Work left-to-right, top-to-bottom (systematic sweep)
    2. Perimeter rooms first, then interior
    3. Hospital: every other room; MOB/clinic: every 2 rooms
    4. Zig-zag pattern across rows
    5. Skip corridors, bathrooms, stairwells (aspect ratio + size filters)
    6. Redundancy: no room >5% floor width from nearest gateway
    7. When unsure, add extra gateways for reliability
    """
    if not rooms:
        return []

    facility = classify_facility(rooms)

    areas = sorted([r["area"] for r in rooms])
    n = len(areas)
    median_area = areas[n // 2]
    p25_area = areas[max(0, int(n * 0.25))]  # small rooms threshold
    p75_area = areas[min(n-1, int(n * 0.75))]  # large space threshold

    # ── Step 1: Filter invalid spaces ────────────────────
    # Remove corridors (elongated), bathrooms/closets (tiny), large lobbies
    valid = []
    for r in rooms:
        # Skip very elongated shapes (corridors, hallways)
        if r["aspect"] > 3.5:
            continue
        # Skip tiny spaces (bathrooms, closets, vestibules) — below 25th percentile
        if r["area"] < p25_area * 0.5:
            continue
        # Skip very large open spaces (lobbies, waiting rooms, large corridors)
        if r["area"] > p75_area * 5.0:
            continue
        valid.append(r)

    if not valid:
        valid = rooms

    # ── Step 2: Detect perimeter vs interior rooms ────────
    all_rx = [r["rx"] for r in valid]
    all_ry = [r["ry"] for r in valid]
    rx_min, rx_max = min(all_rx), max(all_rx)
    ry_min, ry_max = min(all_ry), max(all_ry)
    rx_span = (rx_max - rx_min) or 1
    ry_span = (ry_max - ry_min) or 1
    PERIM_BAND = 0.15  # outer 15% of bounding box = perimeter

    perimeter, interior = [], []
    for r in valid:
        on_perim = (
            r["rx"] < rx_min + rx_span * PERIM_BAND or
            r["rx"] > rx_max - rx_span * PERIM_BAND or
            r["ry"] < ry_min + ry_span * PERIM_BAND or
            r["ry"] > ry_max - ry_span * PERIM_BAND
        )
        (perimeter if on_perim else interior).append(r)

    # ── Step 3: Sort both groups into zig-zag order ───────
    # Left→right on even rows, right→left on odd rows
    ROW_H = 0.04
    def sort_key(r):
        row = int(r["ry"] / ROW_H)
        return (row, r["rx"] if row % 2 == 0 else -r["rx"])

    perimeter = sorted(perimeter, key=sort_key)
    interior  = sorted(interior,  key=sort_key)

    # ── Step 4: Apply placement stride ───────────────────
    # Perimeter: every room (guideline: outline perimeter first, full coverage)
    # Interior hospital: every other room
    # Interior MOB/clinic: every 2 rooms (thinner walls = less density needed)
    stride = 1 if facility == 'hospital' else 2

    selected = list(perimeter)  # all perimeter rooms
    for i, r in enumerate(interior):
        if i % stride == 0:
            selected.append(r)

    # ── Step 5: Dedup — remove double-pins in same room ──
    # Two centroids within 2% of floor width = same physical room
    MIN_DEDUP = 0.022
    deduped = []
    for r in selected:
        if not any(((r["rx"]-s["rx"])**2+(r["ry"]-s["ry"])**2)**0.5 < MIN_DEDUP for s in deduped):
            deduped.append(r)

    # ── Step 6: Redundancy pass ───────────────────────────
    # Any valid room more than COVERAGE_DIST from nearest gateway gets one added
    # This ensures 1-2 gateway failures don't create dead zones
    COVERAGE_DIST = 0.055  # ~5.5% of floor width
    for r in valid:
        nearest = min(
            (((r["rx"]-s["rx"])**2+(r["ry"]-s["ry"])**2)**0.5 for s in deduped),
            default=999
        )
        if nearest > COVERAGE_DIST:
            deduped.append(r)

    # Final sort for clean labeling order (left→right, top→bottom)
    deduped.sort(key=lambda r: (int(r["ry"]/ROW_H), r["rx"]))

    return deduped

def validate_with_claude(rooms, overview_b64, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    candidate_list = "\n".join(f"{i}: rx={r['rx']:.3f}, ry={r['ry']:.3f}" for i, r in enumerate(rooms))
    prompt = f"""You are reviewing BLE gateway placements on a hospital floor plan.
OpenCV detected {len(rooms)} candidate room centroids. Filter out any that are:
- Outside the building boundary (white space around the building)
- In corridors, hallways, vestibules, waiting rooms
- In restrooms, stairwells, elevators, mechanical rooms
Candidates (rx/ry = 0.0-1.0 fractions of image width/height):
{candidate_list}
Return ONLY a JSON array of valid indices to keep, e.g. [0,1,3,5]. No explanation, no markdown."""
    resp = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=2000,
        temperature=0,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":overview_b64}},
            {"type":"text","text":prompt}
        ]}]
    )
    raw = resp.content[0].text
    s, e = raw.find("["), raw.rfind("]")
    if s < 0 or e < 0:
        return rooms
    indices = json.loads(raw[s:e+1])
    return [rooms[i] for i in indices if 0 <= i < len(rooms)]

@app.route("/", methods=["GET"])
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway-planner.html"))

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "opencv": cv2.__version__})

@app.route("/debug", methods=["POST"])
def debug():
    try:
        img_bytes = request.files["image"].read()
        arr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, blockSize=11, C=3)
        kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (3,3))
        closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_close, iterations=3)
        kernel_dilate = cv2.getStructuringElement(cv2.MORPH_RECT, (2,2))
        walls = cv2.dilate(closed, kernel_dilate, iterations=1)
        floor = cv2.bitwise_not(walls)
        flood_mask = np.zeros((h+2, w+2), np.uint8)
        floor_copy = floor.copy()
        for pt in [(0,0),(w-1,0),(0,h-1),(w-1,h-1),(w//2,0),(w//2,h-1),(0,h//2),(w-1,h//2)]:
            if floor_copy[pt[1], pt[0]] == 255:
                cv2.floodFill(floor_copy, flood_mask, pt, 0)
        num_labels, labels, stats_cv, centroids = cv2.connectedComponentsWithStats(floor_copy, connectivity=4)
        total_px = h * w
        viz = img.copy()
        passed = failed_size = failed_aspect = 0
        for i in range(1, num_labels):
            area = stats_cv[i, cv2.CC_STAT_AREA]
            rw = stats_cv[i, cv2.CC_STAT_WIDTH]
            rh = stats_cv[i, cv2.CC_STAT_HEIGHT]
            cx, cy = int(centroids[i][0]), int(centroids[i][1])
            aspect = max(rw,rh) / max(min(rw,rh),1)
            if area < total_px*0.00006 or area > total_px*0.08:
                failed_size += 1
                cv2.circle(viz,(cx,cy),4,(0,0,255),-1)
            elif aspect > 6:
                failed_aspect += 1
                cv2.circle(viz,(cx,cy),4,(0,165,255),-1)
            else:
                passed += 1
                cv2.circle(viz,(cx,cy),7,(0,220,0),-1)
        cv2.putText(viz, f"Valid:{passed} Filtered:{failed_size} Corridor:{failed_aspect}", (10,40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,0,0), 3)
        cv2.putText(viz, f"Valid:{passed} Filtered:{failed_size} Corridor:{failed_aspect}", (10,40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255,255,255), 1)
        _, buf = cv2.imencode(".png", viz)
        return Response(buf.tobytes(), mimetype="image/png")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/detect", methods=["POST"])
def detect():
    try:
        if "image" not in request.files:
            return jsonify({"error": "No image file"}), 400
        img_bytes = request.files["image"].read()
        api_key = request.form.get("api_key", ANTHROPIC_API_KEY)
        do_validate = request.form.get("validate", "true").lower() == "true"
        rooms = detect_rooms(img_bytes)
        if not rooms:
            return jsonify({"error": "No rooms detected.", "pins": []}), 200
        selected = select_gateways(rooms)
        if do_validate and api_key:
            arr = np.frombuffer(img_bytes, np.uint8)
            img_cv = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            h, w = img_cv.shape[:2]
            sc = min(1.0, 1000/max(w,h))
            small = cv2.resize(img_cv, (int(w*sc), int(h*sc)))
            _, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 85])
            overview_b64 = base64.b64encode(buf).decode()
            selected = validate_with_claude(selected, overview_b64, api_key)
        pins = [{"rx":r["rx"],"ry":r["ry"],"label":f"GW-{str(i+1).zfill(2)}","status":"green"} for i,r in enumerate(selected)]
        return jsonify({"pins":pins,"total_rooms_detected":len(rooms),"selected":len(selected)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print("Gateway Planner backend running")
    print(f"OpenCV version: {cv2.__version__}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=False)
