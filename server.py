import cv2
import numpy as np
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
import anthropic
import base64
import json
import os
import sys
import traceback

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
        rw = stats[i, cv2.CC_STAT_WIDTH]
        rh = stats[i, cv2.CC_STAT_HEIGHT]
        cx, cy = centroids[i]
        if area < total_px * 0.00006 or area > total_px * 0.08:
            continue
        aspect = max(rw, rh) / max(min(rw, rh), 1)
        if aspect > 6:
            continue
        rooms.append({"rx": float(cx/w), "ry": float(cy/h), "area": int(area), "aspect": float(aspect)})
    return rooms

def classify_facility(rooms):
    if len(rooms) > 80:
        return 'hospital'
    areas = sorted([r["area"] for r in rooms])
    median = areas[len(areas)//2] if areas else 0
    return 'mob' if median > 3000 else 'hospital'

def select_gateways(rooms):
    if not rooms:
        return []
    facility = classify_facility(rooms)
    areas = sorted([r["area"] for r in rooms])
    n = len(areas)
    p25_area = areas[max(0, int(n*0.25))]
    p75_area = areas[min(n-1, int(n*0.75))]
    valid = []
    for r in rooms:
        if r["aspect"] > 3.5: continue
        if r["area"] < p25_area * 0.5: continue
        if r["area"] > p75_area * 5.0: continue
        valid.append(r)
    if not valid:
        valid = rooms
    all_rx = [r["rx"] for r in valid]
    all_ry = [r["ry"] for r in valid]
    rx_min, rx_max = min(all_rx), max(all_rx)
    ry_min, ry_max = min(all_ry), max(all_ry)
    rx_span = (rx_max - rx_min) or 1
    ry_span = (ry_max - ry_min) or 1
    PERIM_BAND = 0.15
    perimeter, interior = [], []
    for r in valid:
        on_perim = (r["rx"] < rx_min + rx_span*PERIM_BAND or r["rx"] > rx_max - rx_span*PERIM_BAND or
                    r["ry"] < ry_min + ry_span*PERIM_BAND or r["ry"] > ry_max - ry_span*PERIM_BAND)
        (perimeter if on_perim else interior).append(r)
    ROW_H = 0.04
    def sort_key(r):
        row = int(r["ry"] / ROW_H)
        return (row, r["rx"] if row % 2 == 0 else -r["rx"])
    perimeter = sorted(perimeter, key=sort_key)
    interior = sorted(interior, key=sort_key)
    stride = 1 if facility == 'hospital' else 2
    selected = list(perimeter)
    for i, r in enumerate(interior):
        if i % stride == 0:
            selected.append(r)
    MIN_DEDUP = 0.022
    deduped = []
    for r in selected:
        if not any(((r["rx"]-s["rx"])**2+(r["ry"]-s["ry"])**2)**0.5 < MIN_DEDUP for s in deduped):
            deduped.append(r)
    COVERAGE_DIST = 0.055
    for r in valid:
        nearest = min((((r["rx"]-s["rx"])**2+(r["ry"]-s["ry"])**2)**0.5 for s in deduped), default=999)
        if nearest > COVERAGE_DIST:
            deduped.append(r)
    deduped.sort(key=lambda r: (int(r["ry"]/ROW_H), r["rx"]))
    return deduped

def label_gateways_with_claude(selected, overview_b64, api_key):
    try:
        client = anthropic.Anthropic(api_key=api_key)
        candidate_list = "\n".join(f"{i}: rx={r['rx']:.3f}, ry={r['ry']:.3f}" for i, r in enumerate(selected))
        prompt = f"""You are analyzing a floor plan image. I have placed markers at specific coordinates.
For each coordinate, look at that exact location and identify the room name or number printed there.

Coordinates (rx=left-to-right 0.0-1.0, ry=top-to-bottom 0.0-1.0):
{candidate_list}

Naming rules:
- Patient Room / PR + number -> use "PR [number]"
- Nurse Station / NS -> use "NS"
- Office + number -> use "Office [number]"
- Room number visible (e.g. 2210) -> use that number
- Conference Room -> use "Conf"
- Corridor / Hallway -> use "Corridor"
- Cannot identify -> use null

Return ONLY a JSON array, one entry per coordinate in the same order:
[{{"index": 0, "room": "PR 305"}}, {{"index": 1, "room": "NS"}}, {{"index": 2, "room": null}}]"""

        resp = client.messages.create(
            model="claude-opus-4-20250514",
            max_tokens=4000,
            temperature=0,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": overview_b64}},
                {"type": "text", "text": prompt}
            ]}]
        )
        raw = resp.content[0].text
        print(f"DEBUG Claude response: {raw[:500]}", file=sys.stderr)
        s, e = raw.find("["), raw.rfind("]")
        if s < 0 or e < 0:
            return selected
        room_data = json.loads(raw[s:e+1])
        room_counts = {}
        for entry in room_data:
            room = entry.get("room")
            if room:
                room_counts[room] = room_counts.get(room, 0) + 1
        room_seen = {}
        labeled = [dict(r) for r in selected]
        for entry in room_data:
            idx = entry.get("index")
            room = entry.get("room")
            if idx is None or idx >= len(labeled):
                continue
            if room:
                room_seen[room] = room_seen.get(room, 0) + 1
                gw_num = room_seen[room]
                labeled[idx]["label"] = f"{room} GW-{gw_num}" if room_counts[room] > 1 else f"{room} GW-1"
        print(f"DEBUG labels: {[r.get('label') for r in labeled[:5]]}", file=sys.stderr)
        return labeled
    except Exception as ex:
        print(f"DEBUG error: {ex}\n{traceback.format_exc()}", file=sys.stderr)
        return selected

@app.route("/", methods=["GET"])
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway-planner.html"))

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "opencv": cv2.__version__})

@app.route("/detect", methods=["POST"])
def detect():
    try:
        if "image" not in request.files:
            return jsonify({"error": "No image file provided"}), 400
        img_bytes = request.files["image"].read()
        api_key = request.form.get("api_key", ANTHROPIC_API_KEY)
        do_validate = request.form.get("validate", "true").lower() == "true"
        rooms = detect_rooms(img_bytes)
        if not rooms:
            return jsonify({"error": "No rooms detected.", "pins": []}), 200
        selected = select_gateways(rooms)
        for i, r in enumerate(selected):
            r["label"] = f"GW-{str(i+1).zfill(2)}"
        print(f"DEBUG do_validate={do_validate} api_key_present={bool(api_key)} rooms={len(rooms)} selected={len(selected)}", file=sys.stderr)
        if do_validate and api_key:
            arr = np.frombuffer(img_bytes, np.uint8)
            img_cv = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            h, w = img_cv.shape[:2]
            print(f"DEBUG calling label_gateways_tiled", file=sys.stderr)
            selected = label_gateways_tiled(selected, img_cv, api_key)
        pins = [{"rx": r["rx"], "ry": r["ry"], "label": r.get("label", f"GW-{str(i+1).zfill(2)}"), "status": "green"} for i, r in enumerate(selected)]
        return jsonify({"pins": pins, "total_rooms_detected": len(rooms), "selected": len(selected)})
    except Exception as e:
        print(f"DEBUG detect error: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return jsonify({"error": str(e)}), 500

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
        flood_mask = np.zeros((h+2,w+2), np.uint8)
        floor_copy = floor.copy()
        for pt in [(0,0),(w-1,0),(0,h-1),(w-1,h-1),(w//2,0),(w//2,h-1),(0,h//2),(w-1,h//2)]:
            if floor_copy[pt[1],pt[0]] == 255:
                cv2.floodFill(floor_copy, flood_mask, pt, 0)
        num_labels, labels, stats_cv, centroids = cv2.connectedComponentsWithStats(floor_copy, connectivity=4)
        total_px = h*w
        viz = img.copy()
        passed = failed_size = failed_aspect = 0
        for i in range(1, num_labels):
            area = stats_cv[i, cv2.CC_STAT_AREA]
            rw = stats_cv[i, cv2.CC_STAT_WIDTH]
            rh = stats_cv[i, cv2.CC_STAT_HEIGHT]
            cx, cy = int(centroids[i][0]), int(centroids[i][1])
            aspect = max(rw,rh)/max(min(rw,rh),1)
            if area < total_px*0.00006 or area > total_px*0.08:
                failed_size += 1
                cv2.circle(viz,(cx,cy),4,(0,0,255),-1)
            elif aspect > 6:
                failed_aspect += 1
                cv2.circle(viz,(cx,cy),4,(0,165,255),-1)
            else:
                passed += 1
                cv2.circle(viz,(cx,cy),7,(0,220,0),-1)
        label_text = f"Valid:{passed}  Filtered:{failed_size}  Corridor:{failed_aspect}"
        cv2.putText(viz, label_text, (10,40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,0,0), 3)
        cv2.putText(viz, label_text, (10,40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255,255,255), 1)
        _, buf = cv2.imencode(".png", viz)
        return Response(buf.tobytes(), mimetype="image/png")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print("Gateway Planner backend running")
    print(f"OpenCV version: {cv2.__version__}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=False)


def label_gateways_tiled(selected, img_cv, api_key):
    """
    Labels gateways by splitting floor plan into tiles and asking Claude
    to identify room names in each tile at high resolution.
    """
    if not selected:
        return selected

    h, w = img_cv.shape[:2]
    COLS, ROWS = 3, 3
    tile_w = w // COLS
    tile_h = h // ROWS
    labeled = [dict(r) for r in selected]
    client = anthropic.Anthropic(api_key=api_key)

    for row in range(ROWS):
        for col in range(COLS):
            x0, y0 = col * tile_w, row * tile_h
            x1 = min(x0 + tile_w, w)
            y1 = min(y0 + tile_h, h)

            # Find gateways in this tile (with small overlap)
            overlap = 0.02
            tile_gws = [
                (i, r) for i, r in enumerate(selected)
                if (x0/w - overlap) <= r["rx"] <= (x1/w + overlap) and
                   (y0/h - overlap) <= r["ry"] <= (y1/h + overlap)
            ]
            if not tile_gws:
                continue

            # Crop and encode tile at full resolution
            tile_img = img_cv[y0:y1, x0:x1]
            _, buf = cv2.imencode(".jpg", tile_img, [cv2.IMWRITE_JPEG_QUALITY, 95])
            tile_b64 = base64.b64encode(buf).decode()
            tw = x1 - x0
            th = y1 - y0

            # Convert global rx/ry to tile-local coordinates
            candidate_list = "\n".join(
                f"{i}: rx={((r['rx']*w - x0)/tw):.3f}, ry={((r['ry']*h - y0)/th):.3f}"
                for i, r in tile_gws
            )

            prompt = f"""You are analyzing a section of a hospital floor plan image.
I have placed markers at these coordinates within THIS tile image (rx/ry 0.0-1.0 of tile):
{candidate_list}

For each marker, look at that exact position and read the room name or number.
Most rooms have text printed inside them — room numbers, names like "Patient Room", "Office", etc.

Return ONLY a JSON array:
[{{"index": INDEX, "room": "room name or number or null"}}]
Use the original index numbers shown above."""

            try:
                resp = client.messages.create(
                    model="claude-opus-4-20250514",
                    max_tokens=2000,
                    temperature=0,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": tile_b64}},
                        {"type": "text", "text": prompt}
                    ]}]
                )
                raw = resp.content[0].text
                print(f"DEBUG tile({row},{col}) response: {raw[:200]}", file=sys.stderr)
                s, e = raw.find("["), raw.rfind("]")
                if s < 0 or e < 0:
                    continue
                room_data = json.loads(raw[s:e+1])
                for entry in room_data:
                    idx = entry.get("index")
                    room = entry.get("room")
                    if idx is not None and idx < len(labeled) and room:
                        labeled[idx]["label"] = room
            except Exception as ex:
                print(f"DEBUG tile error: {ex}", file=sys.stderr)
                continue

    # Add GW number suffix for duplicate room names
    label_counts = {}
    for r in labeled:
        lbl = r.get("label", "")
        if not lbl.startswith("GW-"):
            label_counts[lbl] = label_counts.get(lbl, 0) + 1
    label_seen = {}
    for r in labeled:
        lbl = r.get("label", "")
        if not lbl.startswith("GW-"):
            label_seen[lbl] = label_seen.get(lbl, 0) + 1
            n = label_seen[lbl]
            if label_counts[lbl] > 1:
                r["label"] = f"{lbl} GW-{n}"
            else:
                r["label"] = f"{lbl} GW-1"

    print(f"DEBUG tiled labels sample: {[r.get('label') for r in labeled[:5]]}", file=sys.stderr)
    return labeled
