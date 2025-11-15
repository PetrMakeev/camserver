# web_server.py
import os
import threading
import time
import shutil
import flask
from flask import Flask, Response, render_template_string
from pathlib import Path
from PIL import Image
import io
from main import resource_path

# ----------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------
CAPTURE_ROOT = "capture"
REFRESH_INTERVAL = 0.5  # 0.3
JPEG_QUALITY = 80
HOST = "0.0.0.0"
PORT = 5000
DEBUG = False

# ----------------------------------------------------------------------
# Flask
# ----------------------------------------------------------------------
app = Flask(__name__)

# === ГЛАВНАЯ СТРАНИЦА: ТОЛЬКО ССЫЛКИ НА VLC ===
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

# === Конвертация в JPEG ===
def load_and_convert_to_jpeg(image_path):
    try:
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return buf.getvalue()
    except Exception as e:
        print(f"[JPEG] Ошибка: {e}")
        return None

# === Генератор MJPEG ===
def generate_mjpeg(cam_id):
    last_mtime = 0
    nocam = resource_path(os.path.join("resource", "nocam.png"))
    noconnect = resource_path(os.path.join("resource", "noconnect.png"))

    while True:
        path = os.path.join(CAPTURE_ROOT, f"cam{cam_id}", "current.png")
        placeholder = None

        if not os.path.exists(path):
            placeholder = nocam if os.path.exists(nocam) else noconnect
        else:
            current_mtime = os.path.getmtime(path)
            if current_mtime <= last_mtime:
                time.sleep(REFRESH_INTERVAL)
                continue
            last_mtime = current_mtime  # Обновляем только при реальном изменении

        jpeg_data = load_and_convert_to_jpeg(placeholder or path)
        if jpeg_data:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n'
                   b'Content-Length: ' + str(len(jpeg_data)).encode() + b'\r\n\r\n' +
                   jpeg_data + b'\r\n')
        else:
            time.sleep(REFRESH_INTERVAL)

# === Маршруты ===
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
        headers={'Cache-Control': 'no-cache', 'Connection': 'close'}
    )

@app.route('/shutdown')
def shutdown():
    # Повторно копируем заглушку + обновляем mtime
    nocam_path = resource_path(os.path.join("resource", "nocam.png"))
    if os.path.exists(nocam_path):
        for cam_id in range(1, 10):
            target = os.path.join(CAPTURE_ROOT, f"cam{cam_id}", "current.png")
            try:
                if os.path.exists(os.path.dirname(target)):
                    shutil.copy2(nocam_path, target)  # Сохраняем метаданные
                    os.utime(target, None)  # <<< ГАРАНТИРОВАННО обновляем mtime
            except Exception as e:
                print(f"[SHUTDOWN] Ошибка копирования в cam{cam_id}: {e}")

    # Даем генератору отправить кадр
    time.sleep(1)

    func = flask.request.environ.get('werkzeug.server.shutdown')
    if func:
        func()
    return "Сервер завершается...", 200

# === Запуск ===
def run_server():
    print(f"[WEB] VLC-потоки: http://localhost:{PORT}/stream/cam1 ... /stream/cam9")
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
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[WEB] Остановлено.")