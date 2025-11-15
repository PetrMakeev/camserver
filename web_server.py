# web_server.py
import os
import threading
import time
import shutil
import flask
import logging
from flask import Flask, Response, render_template_string, request, jsonify
from pathlib import Path
from PIL import Image
import io
from queue import Queue

# ----------------------------------------------------------------------
# Импорт resource_path
# ----------------------------------------------------------------------
try:
    from main import resource_path
except ImportError:
    def resource_path(relative_path):
        return os.path.join(os.path.abspath("."), relative_path)

# ----------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------
CAPTURE_ROOT = "capture"
REFRESH_INTERVAL = 0.5
JPEG_QUALITY = 80
HOST = "0.0.0.0"
PORT = 5000
DEBUG = False

# Глобальные переменные
CAM_URLS = [None] * 9
LAST_UPDATE_TIME = 0
URL_UPDATE_QUEUE = None

def set_update_queue(queue: Queue):
    global URL_UPDATE_QUEUE
    URL_UPDATE_QUEUE = queue
    print("[WEB] Очередь обновлений URL получена")

# ----------------------------------------------------------------------
# Flask + логирование
# ----------------------------------------------------------------------
app = Flask(__name__)

# Логирование в файл
if not DEBUG:
    handler = logging.FileHandler('capture.log')
    handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

# ----------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# MJPEG
# ----------------------------------------------------------------------
def load_and_convert_to_jpeg(image_path):
    try:
        if not os.path.exists(image_path):
            app.logger.warning(f"Файл не найден: {image_path}")
            return None
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return buf.getvalue()
    except Exception as e:
        app.logger.error(f"[JPEG] Ошибка: {e} | Файл: {image_path}")
        return None

def generate_mjpeg(cam_id):
    last_mtime = 0
    nocam_path = resource_path(os.path.join("resource", "nocam.png"))

    while True:
        path = os.path.join(CAPTURE_ROOT, f"cam{cam_id}", "current.png")
        use_path = path

        if not os.path.exists(path):
            use_path = nocam_path
            if not os.path.exists(use_path):
                time.sleep(REFRESH_INTERVAL)
                continue

        current_mtime = os.path.getmtime(use_path)
        if current_mtime <= last_mtime:
            time.sleep(REFRESH_INTERVAL)
            continue
        last_mtime = current_mtime

        jpeg_data = load_and_convert_to_jpeg(use_path)
        if jpeg_data:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n'
                   b'Content-Length: ' + str(len(jpeg_data)).encode() + b'\r\n\r\n' +
                   jpeg_data + b'\r\n')
        else:
            time.sleep(REFRESH_INTERVAL)

# ----------------------------------------------------------------------
# Маршруты
# ----------------------------------------------------------------------
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
        headers={'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
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

        logging.info(f"API запрос: set_urls → {new_urls}")

        if URL_UPDATE_QUEUE is not None:
            for i, url in enumerate(new_urls):
                URL_UPDATE_QUEUE.put((i + 1, url))
            print(f"[API] Отправлено 9 отдельных обновлений URL")

        return jsonify({
            "status": "ok",
            "updated": [f"cam{i+1}" for i, u in enumerate(new_urls) if u]
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/shutdown')
def shutdown():
    nocam_path = resource_path(os.path.join("resource", "nocam.png"))
    if os.path.exists(nocam_path):
        for cam_id in range(1, 10):
            target = os.path.join(CAPTURE_ROOT, f"cam{cam_id}", "current.png")
            try:
                if os.path.exists(os.path.dirname(target)):
                    shutil.copy2(nocam_path, target)
                    os.utime(target, None)  # ← ГАРАНТИЯ mtime
                    app.logger.info(f"[SHUTDOWN] Заглушка -> cam{cam_id}")
            except Exception as e:
                app.logger.error(f"[SHUTDOWN] Ошибка: {e}")

    # Даём генератору отправить кадр
    time.sleep(2)

    func = flask.request.environ.get('werkzeug.server.shutdown')
    if func:
        func()
    return "Сервер завершается...", 200

# ----------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------
def run_server():
    print(f"[WEB] VLC-потоки: http://localhost:{PORT}/stream/cam1 ... /stream/cam9")
    print(f"[WEB] API: POST http://localhost:{PORT}/api/set_urls")
    print(f"[WEB] Завершение: http://localhost:{PORT}/shutdown")
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
        print("\n[WEB] Остановлено.")