"""
cv_node.py — MedCart bed number detection node
Pygame live preview with threaded OCR for smooth camera feed.

Run:
    python3 nodes/cv_node.py             ← MQTT headless mode
    python3 nodes/cv_node.py --preview   ← live camera window

Controls:
    S      — manual scan
    Q/ESC  — quit
"""

import os
import sys
import re
import json
import time
import signal
import logging
import threading
from datetime import datetime

import cv2
import pytesseract
from PIL import Image
import numpy as np
import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

MQTT_BROKER    = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT      = int(os.getenv("MQTT_PORT", "1883"))
CAMERA_INDEX   = int(os.getenv("CAMERA_INDEX", "0"))

SUB_TRIGGER    = "medcart/cv/trigger"
PUB_DETECTED   = "medcart/cv/bed_detected"
PUB_ERROR      = "medcart/cv/error"

CONFIRM_COUNT  = 2
SCAN_INTERVAL  = 2.0   # seconds between auto-scans

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] cv_node — %(message)s"
)
log = logging.getLogger("cv_node")


# ─── Bed number extraction ────────────────────────────────────────────────────

def extract_bed_number(text: str) -> str | None:
    text = text.strip()
    match = re.search(r'\bbed\s*(\d+)\b', text, re.IGNORECASE)
    if match:
        return match.group(1)
    if len(text) <= 4:
        match = re.match(r'^(\d{1,3})$', text.strip())
        if match:
            return match.group(1)
    return None


# ─── OpenCV processing ────────────────────────────────────────────────────────

def find_sign_region(frame):
    """Find white rectangular sign. Returns (crop, bbox) or (None, None)."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(
        thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return None, None

    frame_area = frame.shape[0] * frame.shape[1]
    candidates = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < frame_area * 0.05:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        aspect = w / h
        if not (0.3 <= aspect <= 4.0):
            continue
        candidates.append((area, x, y, w, h))

    if not candidates:
        return None, None

    candidates.sort(reverse=True)
    _, x, y, w, h = candidates[0]
    pad = 10
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(frame.shape[1], x + w + pad)
    y2 = min(frame.shape[0], y + h + pad)
    return frame[y1:y2, x1:x2], (x1, y1, x2, y2)


def preprocess_for_ocr(region):
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.fastNlMeansDenoising(gray, h=10)
    thresh = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 11, 2
    )
    return thresh


def run_ocr(region) -> str:
    processed = preprocess_for_ocr(region)
    pil_img   = Image.fromarray(processed)

    # Get detailed data including confidence per word
    import pytesseract
    data = pytesseract.image_to_data(
        pil_img,
        config="--psm 6 --oem 3",
        output_type=pytesseract.Output.DICT
    )

    # Only keep words with confidence > 60%
    high_conf_words = []
    for i, word in enumerate(data['text']):
        conf = int(data['conf'][i])
        if conf > 60 and word.strip():
            high_conf_words.append(word.strip())

    text = " ".join(high_conf_words)
    log.debug("High-conf OCR words: %r", high_conf_words)
    return text

# ─── Camera ───────────────────────────────────────────────────────────────────

class BedScanner:
    def __init__(self, camera_index: int):
        self.cap = cv2.VideoCapture(camera_index)
        if not self.cap.isOpened():
            log.error("Cannot open camera index %d", camera_index)
            sys.exit(1)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        log.info("Camera opened (index %d)", camera_index)

    def capture_frame(self):
        ret, frame = self.cap.read()
        return frame if ret else None

    def scan_frame(self, frame) -> tuple[str | None, float]:
        """Scan a pre-captured frame."""
        region, bbox = find_sign_region(frame)
        target = region if region is not None else frame
        text   = run_ocr(target)
        bed    = extract_bed_number(text)
        if bed:
            return bed, 0.99
        return None, 0.0

    def confirmed_scan(self) -> tuple[str | None, float]:
        readings = []
        for _ in range(CONFIRM_COUNT + 1):
            frame = self.capture_frame()
            if frame is not None:
                bed, conf = self.scan_frame(frame)
                if bed:
                    readings.append(bed)
            time.sleep(0.5)
        if not readings:
            return None, 0.0
        bed_counts = {}
        for bed in readings:
            bed_counts[bed] = bed_counts.get(bed, 0) + 1
        best_bed = max(bed_counts, key=bed_counts.get)
        if bed_counts[best_bed] >= CONFIRM_COUNT and best_bed.isdigit():
            return best_bed, 0.99
        return None, 0.0

    def release(self):
        self.cap.release()


# ─── OCR worker thread ────────────────────────────────────────────────────────

class OCRWorker:
    """
    Runs OCR in a background thread so camera display stays smooth.
    Camera thread captures frames and puts them in a queue.
    OCR thread reads from queue and updates result.
    """
    def __init__(self, scanner: BedScanner, on_detect=None):
        self.scanner   = scanner
        self.on_detect = on_detect   # callback when bed detected
        self.result    = None        # latest detected bed number
        self.bbox      = None        # latest bounding box
        self.scanning  = False       # True while OCR running
        self._frame    = None        # shared frame
        self._lock     = threading.Lock()
        self._running  = True
        self._thread   = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def update_frame(self, frame):
        """Called from camera thread with latest frame."""
        with self._lock:
            self._frame = frame.copy()

    def get_bbox(self):
        """Get latest bounding box for display."""
        with self._lock:
            return self.bbox

    def _loop(self):
        last_scan_t    = 0
        no_detect_count = 0   # count consecutive failed scans

        while self._running:
            now = time.time()
            if now - last_scan_t < SCAN_INTERVAL:
                time.sleep(0.05)
                continue

            with self._lock:
                frame = self._frame.copy() if self._frame is not None else None

            if frame is None:
                time.sleep(0.1)
                continue

            self.scanning = True
            last_scan_t   = now

            region, bbox = find_sign_region(frame)
            with self._lock:
                self.bbox = bbox

            target = region if region is not None else frame
            text   = run_ocr(target)
            bed    = extract_bed_number(text)

            if bed:
                no_detect_count = 0
                self.result = bed
                log.info("Bed detected: %s", bed)
                if self.on_detect:
                    self.on_detect(bed)
            else:
                no_detect_count += 1
                # After 3 consecutive failed scans (~6 seconds), reset result
                if no_detect_count >= 3:
                    self.result = None
                    log.info("No bed detected — resetting.")

            self.scanning = False

    def stop(self):
        self._running = False


# ─── Pygame live preview ──────────────────────────────────────────────────────

def run_preview(scanner: BedScanner, mqtt_client=None):
    import pygame

    pygame.init()
    screen = pygame.display.set_mode((640, 480))
    pygame.display.set_caption("MedCart CV — Bed Scanner")
    clock  = pygame.time.Clock()
    font   = pygame.font.SysFont("monospace", 26, bold=True)
    font_s = pygame.font.SysFont("monospace", 16)

    last_detected = None

    def on_detect(bed):
        nonlocal last_detected
        last_detected = bed
        if mqtt_client:
            mqtt_client.publish(PUB_DETECTED, json.dumps({
                "bed_number": bed,
                "confidence": 0.99,
                "timestamp":  datetime.now().isoformat()
            }), qos=1)
            log.info("Published: Bed %s", bed)

    # Start OCR worker thread
    worker = OCRWorker(scanner, on_detect=on_detect)

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            if event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
                if event.key == pygame.K_s:
                    # Force immediate scan
                    log.info("Manual scan triggered")
                    worker._lock.acquire()
                    worker._loop.__func__  # just reset timer
                    worker._lock.release()

        # Capture frame — no OCR here, just display
        frame = scanner.capture_frame()
        if frame is None:
            continue

        # Give frame to OCR worker
        worker.update_frame(frame)

        # Draw bounding box on display frame
        bbox = worker.get_bbox()
        if bbox:
            x1, y1, x2, y2 = bbox
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

        # Convert to pygame surface
        rgb  = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        surf = pygame.surfarray.make_surface(np.transpose(rgb, (1, 0, 2)))
        screen.blit(surf, (0, 0))

        # Status bar
        bar = pygame.Surface((640, 50))
        bar.set_alpha(210)
        bar.fill((0, 0, 0))
        screen.blit(bar, (0, 430))

        if worker.result:
            status = f"BED {worker.result} DETECTED"
            color  = (0, 255, 0)
        elif worker.scanning:
            status = "Scanning…"
            color  = (255, 165, 0)
        else:
            status = "Searching for bed number…"
            color  = (255, 255, 0)

        screen.blit(font.render(status, True, color), (10, 438))
        screen.blit(font_s.render("S: scan  Q: quit", True, (180, 180, 180)), (490, 458))

        pygame.display.flip()
        clock.tick(30)   # 30fps display, OCR runs every 2s in background

    worker.stop()
    pygame.quit()
    scanner.release()


# ─── Continuous headless mode ─────────────────────────────────────────────────

def run_continuous(client: mqtt.Client, scanner: BedScanner):
    log.info("Continuous scan active — every 2 seconds…")
    last_detected      = None
    last_detected_time = 0

    while True:
        frame = scanner.capture_frame()
        # CHANGE THIS LINE BELOW:
        if frame is not None:
            bed, conf = scanner.scan_frame(frame)
            if bed:
                now = time.time()
                if bed != last_detected or (now - last_detected_time) > 10:
                    client.publish(PUB_DETECTED, json.dumps({
                        "bed_number": bed,
                        "confidence": round(conf, 3),
                        "timestamp":  datetime.now().isoformat()
                    }), qos=1)
                    log.info("Auto-published: Bed %s", bed)
                    last_detected      = bed
                    last_detected_time = now
        time.sleep(SCAN_INTERVAL)


# ─── MQTT ─────────────────────────────────────────────────────────────────────

def build_mqtt_client(scanner: BedScanner) -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="cv_node",
        clean_session=True
    )

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected → %s:%d", MQTT_BROKER, MQTT_PORT)
            c.subscribe(SUB_TRIGGER, qos=1)
        else:
            log.error("MQTT connect failed rc=%s", reason_code)

    def on_message(c, userdata, msg):
        log.info("Manual scan triggered via MQTT.")
        def do_scan():
            bed, conf = scanner.confirmed_scan()
            if bed:
                c.publish(PUB_DETECTED, json.dumps({
                    "bed_number": bed,
                    "confidence": round(conf, 3),
                    "timestamp":  datetime.now().isoformat()
                }), qos=1)
            else:
                c.publish(PUB_ERROR, json.dumps({
                    "error":     "no_bed_detected",
                    "message":   "Point camera at bed number sign and try again.",
                    "timestamp": datetime.now().isoformat()
                }), qos=1)
        threading.Thread(target=do_scan, daemon=True).start()

    client.on_connect = on_connect
    client.on_message = on_message
    return client


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    scanner = BedScanner(CAMERA_INDEX)

    if "--preview" in sys.argv:
        try:
            client = build_mqtt_client(scanner)
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            client.loop_start()
            run_preview(scanner, mqtt_client=client)
            client.loop_stop()
        except ConnectionRefusedError:
            log.warning("MQTT not found — running preview without MQTT.")
            run_preview(scanner, mqtt_client=None)
        return

    client = build_mqtt_client(scanner)
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_start()
    except ConnectionRefusedError:
        log.error("MQTT broker not reachable. Start Mosquitto first.")
        scanner.release()
        sys.exit(1)

    def shutdown(sig, frame):
        log.info("Shutting down cv_node…")
        scanner.release()
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    scan_thread = threading.Thread(
        target=run_continuous,
        args=(client, scanner),
        daemon=True
    )
    scan_thread.start()

    log.info("CV node running — continuous scan + manual trigger active.")
    client.loop_forever()


if __name__ == "__main__":
    main()