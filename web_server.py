# web_server.py
import os
import threading
import time
import flask
import logging
from flask import Flask, Response, render_template_string, request, jsonify
from pathlib import Path
from PIL import Image
import io
from queue import Queue

try:
    from main import resource_path, NOCAM_IMAGE_BYTES
except ImportError:
    def resource_path(relative_path):
        return Path.cwd() / relative_path
    NOCAM_IMAGE_BYTES = None

CAPTURE_ROOT = Path("capture")
REFRESH_INTERVAL = 0.5
JPEG_QUALITY = 80
HOST = "0.0.0.0"
PORT = 5000
DEBUG = False

CAM_URLS = [None] * 9
LAST_UPDATE_TIME = 0
URL_UPDATE_QUEUE = None

def set_update_queue(queue: Queue):
    global URL_UPDATE_QUEUE
    URL_UPDATE_QUEUE = queue

app = Flask(__name__)

if not DEBUG:
    handler = logging.FileHandler('capture.log', encoding='utf-8')  # ← ДОБАВЛЕНО encoding='utf-8'
    handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

INDEX_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VLC Потоки</title>
    <style>
        body { font-family: Arial; background:#111; color:#eee; padding:30px; }
        h1 { text-align:center; color:#0af; }
        .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:20px; margin-top:30px; }
        .cam { background:#222; padding:20px; border-radius:10px; text-align:center; }
        .cam h3 { margin:0 0 10px; color:#0c0; }
        .cam a { color:#0af; font-size:1.1em; word-break:break-all; }
    </style>
</head>
<body>
    <h1>VLC Потоки (9 камер)</h1>
    <div class="grid">
        {% for i in range(1, 10) %}
        <div class="cam">
            <h3>Камера {{ i }}</h3>
            <a href="/stream/cam{{ i }}">http://{{ request.host }}/stream/cam{{ i }}</a>
        </div>
        {% endfor %}
    </div>
</body>
</html>
"""

def load_and_convert_to_jpeg(image_bytes):
    try:
        if not image_bytes:
            return None
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception as e:
        app.logger.error(f"[JPEG] Ошибка: {e}")
        return None

def generate_mjpeg(cam_id):
    last_mtime = 0
    current_path = CAPTURE_ROOT / f"cam{cam_id}" / "current.png"

    while True:
        # === ПРИНУДИТЕЛЬНО ПРОВЕРЯЕМ ФАЙЛ КАЖДЫЙ ЦИКЛ ===
        if current_path.exists():
            current_mtime = current_path.stat().st_mtime
            # === ЕСЛИ ФАЙЛ СУЩЕСТВУЕТ — ЧИТАЕМ ===
            try:
                image_bytes = current_path.read_bytes()
            except:
                image_bytes = NOCAM_IMAGE_BYTES
        else:
            image_bytes = NOCAM_IMAGE_BYTES
            current_mtime = time.time()  # ← ФИКТИВНЫЙ mtime

        # === ОТПРАВЛЯЕМ, ЕСЛИ ЕСТЬ ДАННЫЕ ===
        if image_bytes:
            jpeg_data = load_and_convert_to_jpeg(image_bytes)
            if jpeg_data:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n'
                       b'Content-Length: ' + str(len(jpeg_data)).encode() + b'\r\n\r\n' +
                       jpeg_data + b'\r\n')
                # === СБРАСЫВАЕМ mtime ПОСЛЕ ОТПРАВКИ ===
                last_mtime = 0
            else:
                time.sleep(REFRESH_INTERVAL)
                continue
        else:
            time.sleep(REFRESH_INTERVAL)
            continue

        # === ЖДЁМ НОВОГО КАДРА ===
        time.sleep(REFRESH_INTERVAL)

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

@app.route('/stream/cam<int:cam_id>')
def mjpeg_stream(cam_id):
    if cam_id < 1 or cam_id > 9:
        return "Камера не найдена", 404
    return Response(
        generate_mjpeg(cam_id),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
            'Connection': 'close'
        }
    )

@app.route('/api/set_urls', methods=['POST'])
def set_urls():
    global CAM_URLS, LAST_UPDATE_TIME
    now = time.time()
    if now - LAST_UPDATE_TIME < 3:
        return jsonify({"error": "Слишком частые запросы"}), 429

    try:
        data = request.get_json()
        if not data or 'urls' not in data:
            return jsonify({"error": "Ожидается JSON: {'urls': [...]}"}), 400

        urls = data['urls']
        if not isinstance(urls, list) or len(urls) > 9:
            return jsonify({"error": "urls должен быть списком до 9 элементов"}), 400

        new_urls = urls[:9] + [None] * (9 - len(urls))
        CAM_URLS[:] = new_urls
        LAST_UPDATE_TIME = now

        app.logger.info(f"API запрос: set_urls → {new_urls}")

        if URL_UPDATE_QUEUE is not None:
            for i, url in enumerate(new_urls):
                URL_UPDATE_QUEUE.put((i + 1, url))

        return jsonify({
            "status": "ok",
            "updated": [f"cam{i+1}" for i, u in enumerate(new_urls) if u]
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/status')
def status():
    return jsonify({
        "cams": [
            {"id": i+1, "url": CAM_URLS[i] or None}
            for i in range(9)
        ]
    })

@app.route('/shutdown')
def shutdown():
    print("[SHUTDOWN] Копирование заглушки...")
    if NOCAM_IMAGE_BYTES:
        for cam_id in range(1, 10):
            target = CAPTURE_ROOT / f"cam{cam_id}" / "current.png"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(NOCAM_IMAGE_BYTES)
                # === ПРИНУДИТЕЛЬНО МЕНЯЕМ mtime ===
                os.utime(target, (time.time(), time.time()))
                app.logger.info(f"[SHUTDOWN] Заглушка -> cam{cam_id}")
            except Exception as e:
                app.logger.error(f"[SHUTDOWN] Ошибка: {e}")

    print("[SHUTDOWN] Ожидание 6 сек для отправки заглушки...")
    time.sleep(6)  # ← 6 СЕКУНД — ГАРАНТИЯ

    # === ОТПРАВЛЯЕМ FIN КЛИЕНТАМ ===
    print("[SHUTDOWN] Завершение сервера...")
    func = request.environ.get('werkzeug.server.shutdown')
    if func:
        func()
    return "Сервер завершается...", 200

def run_server():
    app.run(host=HOST, port=PORT, debug=DEBUG, threaded=True, use_reloader=False)

def start_web_server():
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    time.sleep(1)
    return thread

if __name__ == "__main__":
    start_web_server()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        pass