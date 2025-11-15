# web_server.py (обновлённая версия)

import os
import threading
import time
from flask import Flask, Response, render_template_string
from pathlib import Path
from PIL import Image
import io
from main import resource_path

# --- Конфигурация ---
CAPTURE_ROOT = "capture"
REFRESH_INTERVAL = 0.5
JPEG_QUALITY = 80
HOST = "0.0.0.0"
PORT = 5000
DEBUG = False

app = Flask(__name__)

# --- Главная страница ---
INDEX_HTML = """... (см. выше) ..."""

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

# --- Чтение current.png ---
def get_current_frame_path(cam_id):
    folder = os.path.join(CAPTURE_ROOT, f"cam{cam_id}")
    path = os.path.join(folder, "current.png")
    return path if os.path.exists(path) else None

# --- Конвертация в JPEG ---
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

# --- MJPEG генератор ---
def generate_mjpeg(cam_id):
    last_mtime = 0
    placeholder_nocam = resource_path(os.path.join("resource", "nocam.png"))
    placeholder_noconnect = resource_path(os.path.join("resource", "noconnect.png"))

    while True:
        frame_path = get_current_frame_path(cam_id)
        use_placeholder = None
        jpeg_data = None

        if not frame_path:
            use_placeholder = placeholder_nocam if os.path.exists(placeholder_nocam) else placeholder_noconnect
        else:
            mtime = os.path.getmtime(frame_path)
            if mtime <= last_mtime:
                time.sleep(REFRESH_INTERVAL)
                continue
            last_mtime = mtime

        if use_placeholder or frame_path:
            jpeg_data = load_and_convert_to_jpeg(use_placeholder or frame_path)

        if jpeg_data:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n'
                   b'Content-Length: ' + str(len(jpeg_data)).encode() + b'\r\n\r\n' +
                   jpeg_data + b'\r\n')
        else:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n'
                   b'Content-Length: 0\r\n\r\n')

        time.sleep(REFRESH_INTERVAL)

# --- ЕДИНСТВЕННЫЙ ПОТОК ДЛЯ КАМЕРЫ ---
@app.route('/cam<int:cam_id>')
def cam_stream(cam_id):
    if cam_id < 1 or cam_id > 9:
        return "Камера не найдена", 404
    return Response(
        generate_mjpeg(cam_id),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
            'Connection': 'close',
            'Content-Disposition': f'inline; filename="cam{cam_id}.mjpg"'
        }
    )

# --- Запуск ---
def run_server():
    print(f"[WEB] Сервер: http://localhost:{PORT}")
    print(f"[WEB] VLC: /cam1 ... /cam9")
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