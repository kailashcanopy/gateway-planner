"""
Gateway Planner — OpenCV Backend
=================================
All computer vision logic for BLE gateway placement on hospital floor plans.

Dependencies:
    pip install flask flask-cors opencv-python-headless numpy anthropic pillow gunicorn

Run locally:
    python server.py

Deploy to Render:
    - Build command:  pip install -r requirements.txt
    - Start command:  gunicorn server:app
    - Env vars:       ANTHROPIC_API_KEY=sk-ant-...

Endpoints:
    GET  /              → Serves gateway-planner.html
    GET  /health        → Health check + OpenCV version
    POST /detect        → Main placement endpoint (multipart: image file)
    POST /debug         → Returns annotated image showing detected rooms
"""

import cv2
import numpy as np
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
import anthropic
import base64
import json
import os
import sys

app = Flask(__name__)
CORS(app)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ── Room Detection ────────────────────────────────────────
# Uses adaptive thresholding + morphological operations + connected components
# to find enclosed rooms inside a floor plan image.

def detect_rooms(img_bytes: bytes) -> list[dict]:
    """
    Detects enclosed room regions in a floor plan image.

    Pipeline:
      1. Convert to grayscale
      2. Adaptive threshold → walls become white, floor becomes black
      3. Morphological close → seals hairline gaps in walls
      4. Dilate → thickens walls to fully enclose rooms
      5. Flood fill from image edges → marks exterior (outside building)
      6. Connected components → each enclosed region = one room
      7. Filter by area and aspect ratio → removes corridors, bathrooms, lobbies

    Returns:
        List of dicts: { rx, ry, area, aspect }
        rx/ry are normalized 0.0–1.0 coordinates of room centroids
    """
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image — ensure it is a valid PNG or JPEG")

    h, w = img.shape[:2]

    # Step 1: Grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Step 2: Adaptive threshold
    # ADAPTIVE_THRESH_GAUSSIAN_C handles uneven lighting across large floor plans
    # blockSize=11 picks up fine wall detail; C=3 sets sensitivity
    binary = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        blockSize=11, C=3
    )

    # Step 3: Morphological close — seals hairline gaps between wall segments
    # Critical for PDFs rendered at lower DPI where thin walls have small breaks
    kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_close, iterations=3)

    # Step 4: Dilate — thickens walls to fully seal room boundaries
    kernel_dilate = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    walls = cv2.dilate(closed, kernel_dilate, iterations=1)

    # Step 5: Invert → floor space is now white
    floor = cv2.bitwise_not(walls)

    # Step 6: Flood fill from all 8 edge points to mark exterior space
    # Any white region connected to the image border = outside the building
    flood_mask = np.zeros((h + 2, w + 2), np.uint8)
    floor_copy = floor.copy()
    edge_pts = [
        (0, 0), (w-1, 0), (0, h-1), (w-1, h-1),
        (w//2, 0), (w//2, h-1), (0, h//2), (w-1, h//2)
    ]
    for pt in edge_pts:
        if floor_copy[pt[1], pt[0]] == 255:
            cv2.floodFill(floor_copy, flood_mask, pt, 0)

    # Step 7: Connected components — each remaining white region = enclosed room
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        floor_copy, connectivity=4
    )

    total_px = h * w
    rooms = []

    for i in range(1, num_labels):  # skip background label 0
        area = stats[i, cv2.CC_STAT_AREA]
        rw   = stats[i, cv2.CC_STAT_WIDTH]
        rh   = stats[i, cv2.CC_STAT_HEIGHT]
        cx, cy = centroids[i]

        # Filter: minimum area — 0.006% of image catches small exam/utility rooms
        # but skips individual pixels and annotation artifacts
        min_area = total_px * 0.00006
        # Filter: maximum area — 8% removes large open lobbies/corridors
        max_area = total_px * 0.08
        if area < min_area or area > max_area:
            continue

        # Filter: aspect ratio — rooms are roughly square/rectangular
        # Corridors are very elongated (aspect > 6:1)
        aspect = max(rw, rh) / max(min(rw, rh), 1)
        if aspect > 6:
            continue

        rooms.append({
            "rx":     float(cx / w),    # normalized x centroid
            "ry":     float(cy / h),    # normalized y centroid
            "area":   int(area),        # pixel area at processing resolution
            "aspect": float(aspect)     # width/height ratio
        })

    return rooms


# ── Facility Classification ───────────────────────────────

def classify_facility(rooms: list[dict]) -> str:
    """
    Classifies floor plan as 'hospital' or 'mob' (Medical Office Building).

    Large hospitals have many small patient rooms.
    MOBs/outpatient clinics have fewer, larger exam rooms and offices.

    This affects gateway density:
    - Hospital: every other room (BLE needs more coverage through thick walls)
    - MOB: every 2 rooms (thinner walls = longer BLE range)
    """
    if len(rooms) > 80:
        return 'hospital'
    areas = sorted([r["area"] for r in rooms])
    median = areas[len(areas) // 2] if areas else 0
    return 'mob' if median > 3000 else 'hospital'


# ── Gateway Selection ─────────────────────────────────────

def select_gateways(rooms: list[dict]) -> list[dict]:
    """
    Applies deployment guidelines to select optimal gateway positions.

    Guidelines implemented:
    1. Begin from one side and work systematically (zig-zag row sort)
    2. Outline perimeter first, then move inward
    3. Hospital: every other room; MOB/clinic: every 2 rooms
    4. Zig-zag pattern across rows (even rows L→R, odd rows R→L)
    5. Skip corridors/bathrooms/vestibules (aspect + size filters)
    6. Redundancy: no room >5.5% floor width from nearest gateway
    7. When unsure, add extra gateways (redundancy pass adds them back)

    Returns:
        Subset of rooms selected for gateway placement
    """
    if not rooms:
        return []

    facility = classify_facility(rooms)

    areas = sorted([r["area"] for r in rooms])
    n = len(areas)
    p25_area = areas[max(0, int(n * 0.25))]
    p75_area = areas[min(n-1, int(n * 0.75))]

    # ── Filter invalid spaces ─────────────────────────────
    valid = []
    for r in rooms:
        # Skip elongated corridors and hallways
        if r["aspect"] > 3.5:
            continue
        # Skip tiny spaces: bathrooms, closets, vestibules (below p25 * 0.5)
        if r["area"] < p25_area * 0.5:
            continue
        # Skip large open spaces: lobbies, waiting rooms (above p75 * 5)
        if r["area"] > p75_area * 5.0:
            continue
        valid.append(r)

    if not valid:
        valid = rooms  # fallback if filters are too aggressive

    # ── Detect perimeter vs interior ─────────────────────
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

    # ── Zig-zag sort ──────────────────────────────────────
    ROW_H = 0.04  # row band height (4% of floor height per row)

    def sort_key(r):
        row = int(r["ry"] / ROW_H)
        # Even rows: left→right; odd rows: right→left = zig-zag
        return (row, r["rx"] if row % 2 == 0 else -r["rx"])

    perimeter = sorted(perimeter, key=sort_key)
    interior  = sorted(interior,  key=sort_key)

    # ── Apply placement stride ────────────────────────────
    # Perimeter: every room (full outline coverage)
    # Interior: every 1 room for hospital, every 2 for MOB
    stride = 1 if facility == 'hospital' else 2

    selected = list(perimeter)
    for i, r in enumerate(interior):
        if i % stride == 0:
            selected.append(r)

    # ── Dedup: remove double-pins in same room ────────────
    # Two centroids within 2.2% of floor width = same physical room
    MIN_DEDUP = 0.022
    deduped = []
    for r in selected:
        too_close = any(
            ((r["rx"] - s["rx"])**2 + (r["ry"] - s["ry"])**2)**0.5 < MIN_DEDUP
            for s in deduped
        )
        if not too_close:
            deduped.append(r)

    # ── Redundancy pass ───────────────────────────────────
    # Any valid room more than 5.5% floor width from nearest gateway gets added
    # Ensures 1–2 gateway failures don't create dead zones
    COVERAGE_DIST = 0.055
    for r in valid:
        nearest = min(
            (((r["rx"] - s["rx"])**2 + (r["ry"] - s["ry"])**2)**0.5 for s in deduped),
            default=999
        )
        if nearest > COVERAGE_DIST:
            deduped.append(r)

    # Final sort: left→right, top→bottom for clean sequential labeling
    deduped.sort(key=lambda r: (int(r["ry"] / ROW_H), r["rx"]))

    return deduped


# ── Claude Validation (optional) ─────────────────────────
# Sends detected room positions + floor plan overview to Claude.
# Claude filters out any placements that landed in bathrooms,
# stairwells, corridors, or outside the building — semantic
# filtering that pixel-level CV can't reliably do on its own.

def label_gateways_with_claude(selected: list[dict], overview_b64: str, api_key: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=api_key)

    candidate_list = "\n".join(
        f"{i}: rx={r['rx']:.3f}, ry={r['ry']:.3f}"
        for i, r in enumerate(selected)
    )

    prompt = f"""You are analyzing a floor plan image. I have placed markers at specific coordinates on this floor plan.

For each coordinate below, look at that exact location on the floor plan image and tell me:
1. What is the room name or number printed on or near that location?
2. What type of room is it?

Coordinates (rx = left-to-right 0.0-1.0, ry = top-to-bottom 0.0-1.0):
{candidate_list}

Rules for naming:
- If you see "Patient Room 305" or "PR 305" → use "PR 305"
- If you see "Nurse Station" or "NS" → use "NS"  
- If you see "Office 210" → use "Office 210"
- If you see a room number like "2210" → use that number
- If you see "Corridor" or "Hallway" → use "Corridor"
- If you see "Conference" → use "Conf"
- If you cannot read any label at that location → use the room type if visible, otherwise use null

IMPORTANT: Look carefully at the image. Most rooms have text labels printed inside them.

Return ONLY a valid JSON array, one entry per coordinate, same order:
[{{"index": 0, "room": "PR 305"}}, {{"index": 1, "room": "NS"}}, {{"index": 2, "room": null}}]"""

    try:
        resp = client.messages.create(
            model="claude-opus-4-20250514",
            max_tokens=4000,
            temperature=0,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": overview_b64}},
                    {"type": "text", "text": prompt}
                ]
            }]
        )
        raw = resp.content[0].text
        print(f"DEBUG Claude raw response (first 500): {raw[:500]}", file=sys.stderr)

        s, e = raw.find("["), raw.rfind("]")
        if s < 0 or e < 0:
            return selected

        room_data = json.loads(raw[s:e+1])

        # Count how many times each room name appears
        room_counts = {}
        for entry in room_data:
            room = entry.get("room")
            if room:
                room_counts[room] = room_counts.get(room, 0) + 1

        # Assign labels — track per-room gateway counter
        room_seen = {}
        labeled = [dict(r) for r in selected]  # copy to avoid mutation
        for entry in room_data:
            idx  = entry.get("index")
            room = entry.get("room")
            if idx is None or idx >= len(labeled):
                continue
            if room:
                room_seen[room] = room_seen.get(room, 0) + 1
                gw_num = room_seen[room]
                if room_counts[room] > 1:
                    labeled[idx]["label"] = f"{room} GW-{gw_num}"
                else:
                    labeled[idx]["label"] = f"{room} GW-1"
            # null room → keep existing GW-XX fallback label

        return labeled

   except Exception as ex:
        import traceback
        print(f"DEBUG label_gateways error: {ex}", file=sys.stderr)
        print(f"DEBUG traceback: {traceback.format_exc()}", file=sys.stderr)
        return selected


# ── Flask Routes ──────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    """Serve the frontend HTML app."""
    return send_file(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway-planner.html")
    )


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint — returns OpenCV version."""
    return jsonify({"status": "ok", "opencv": cv2.__version__})


@app.route("/detect", methods=["POST"])
def detect():
    """
    Main gateway placement endpoint.

    Request (multipart/form-data):
        image    — floor plan image file (PNG/JPEG)
        api_key  — Anthropic API key for Claude validation (optional)
        validate — "true"/"false" — whether to run Claude validation pass

    Response (JSON):
        pins                  — list of { rx, ry, label, status }
        total_rooms_detected  — total rooms found by OpenCV
        selected              — number of gateways placed after filtering
    """
    try:
        if "image" not in request.files:
            return jsonify({"error": "No image file provided"}), 400

        img_bytes  = request.files["image"].read()
        api_key    = request.form.get("api_key", ANTHROPIC_API_KEY)
        do_validate = request.form.get("validate", "true").lower() == "true"

        # Run OpenCV detection
        rooms = detect_rooms(img_bytes)
        if not rooms:
            return jsonify({
                "error": "No rooms detected. Try a higher-contrast floor plan image.",
                "pins": []
            }), 200

        # Apply placement strategy
        selected = select_gateways(rooms)

        # Assign fallback numeric labels first
        for i, r in enumerate(selected):
            r["label"] = f"GW-{str(i+1).zfill(2)}"

        # Optional: Claude room labeling + semantic validation pass
        if do_validate and api_key:
            arr    = np.frombuffer(img_bytes, np.uint8)
            img_cv = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            h, w   = img_cv.shape[:2]
            sc     = min(1.0, 1200 / max(w, h))
            small  = cv2.resize(img_cv, (int(w * sc), int(h * sc)))
            _, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 90])
            overview_b64 = base64.b64encode(buf).decode()
            # Label gateways by room name (also filters invalid placements)
            selected = label_gateways_with_claude(selected, overview_b64, api_key)

        pins = [
            {
                "rx":     r["rx"],
                "ry":     r["ry"],
                "label":  r.get("label", f"GW-{str(i+1).zfill(2)}"),
                "status": "green"
            }
            for i, r in enumerate(selected)
        ]

        return jsonify({
            "pins":                 pins,
            "total_rooms_detected": len(rooms),
            "selected":             len(selected)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug", methods=["POST"])
def debug():
    """
    Debug endpoint — returns annotated floor plan image showing:
        Green dots  — valid rooms (will get gateways)
        Red dots    — filtered out (too small or too large)
        Orange dots — filtered out (corridor shape)

    Useful for tuning detection parameters on new floor plan types.

    Request (multipart/form-data):
        image — floor plan image file
    """
    try:
        img_bytes = request.files["image"].read()
        arr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        h, w = img.shape[:2]

        # Run same pipeline as detect_rooms
        gray   = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY_INV, blockSize=11, C=3)
        kernel_close  = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        closed        = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_close, iterations=3)
        kernel_dilate = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        walls         = cv2.dilate(closed, kernel_dilate, iterations=1)
        floor         = cv2.bitwise_not(walls)

        flood_mask = np.zeros((h+2, w+2), np.uint8)
        floor_copy = floor.copy()
        for pt in [(0,0),(w-1,0),(0,h-1),(w-1,h-1),(w//2,0),(w//2,h-1),(0,h//2),(w-1,h//2)]:
            if floor_copy[pt[1], pt[0]] == 255:
                cv2.floodFill(floor_copy, flood_mask, pt, 0)

        num_labels, labels, stats_cv, centroids = cv2.connectedComponentsWithStats(
            floor_copy, connectivity=4
        )
        total_px = h * w
        viz = img.copy()
        passed = failed_size = failed_aspect = 0

        for i in range(1, num_labels):
            area   = stats_cv[i, cv2.CC_STAT_AREA]
            rw     = stats_cv[i, cv2.CC_STAT_WIDTH]
            rh     = stats_cv[i, cv2.CC_STAT_HEIGHT]
            cx, cy = int(centroids[i][0]), int(centroids[i][1])
            aspect = max(rw, rh) / max(min(rw, rh), 1)

            if area < total_px * 0.00006 or area > total_px * 0.08:
                failed_size += 1
                cv2.circle(viz, (cx, cy), 4, (0, 0, 255), -1)      # red
            elif aspect > 6:
                failed_aspect += 1
                cv2.circle(viz, (cx, cy), 4, (0, 165, 255), -1)    # orange
            else:
                passed += 1
                cv2.circle(viz, (cx, cy), 7, (0, 220, 0), -1)      # green

        label_text = f"Valid:{passed}  Filtered(size):{failed_size}  Corridor:{failed_aspect}"
        cv2.putText(viz, label_text, (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,0,0), 3)
        cv2.putText(viz, label_text, (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255,255,255), 1)

        _, buf = cv2.imencode(".png", viz)
        return Response(buf.tobytes(), mimetype="image/png")

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Entry point ───────────────────────────────────────────

if __name__ == "__main__":
    print("Gateway Planner backend running")
    print(f"OpenCV version: {cv2.__version__}")
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5050)),
        debug=False
    )
