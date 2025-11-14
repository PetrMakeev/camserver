# web_server.py
import os
import threading
import time
from flask import Flask, Response, render_template_string
from pathlib import Path

# ----------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------
CAPTURE_ROOT = "capture"
REFRESH_INTERVAL = 1.0  # секунд между обновлениями
HOST = "0.0.0.0"
PORT = 5000
DEBUG = False

# ----------------------------------------------------------------------
# Flask приложение
# ----------------------------------------------------------------------
app = Flask(__name__)

# HTML шаблон для страницы камеры
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Камера {{ cam_id }}</title>
    <style>
        body, html { margin:0; padding:0; height:100%; background:#000; overflow:hidden; }
        img { width:100%; height:100%; object-fit: contain; }
    </style>
</head>
<body>
    <img src="{{ url_for('video_feed', cam_id=cam_id) }}" alt="Камера {{ cam_id }}">
</body>
</html>
"""

# Главная страница — список камер
INDEX_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Мониторинг камер</title>
    <style>
        body { font-family: Arial, sans-serif; background:#111; color:#eee; padding:20px; }
        h1 { text-align:center; }
        .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap:15px; }
        .cam { background:#222; padding:10px; border-radius:8px; text-align:center; }
        .cam a { color:#0af; text-decoration:none; font-size:1.2em; }
        .cam a:hover { text-decoration:underline; }
    </style>
</head>
<body>
    <h1>Живые камеры (9 шт)</h1>
    <div class="grid">
        {% for i in range(1, 10) %}
        <div class="cam">
            <a href="/cam{{ i }}">Камера {{ i }}</a>
        </div>
        {% endfor %}
    </div>
</body>
</html>
"""

def get_latest_frame(cam_folder):
    frame_path = os.path.join(cam_folder, "current.png")
    return frame_path if os.path.exists(frame_path) else None

def generate_frames(cam_id):
    """Генератор MJPEG потока для одной камеры"""
    cam_folder = os.path.join(CAPTURE_ROOT, f"cam{cam_id}")
    last_mtime = 0

    while True:
        frame_path = get_latest_frame(cam_folder)
        placeholder = None

        # Если нет кадров — используем заглушку
        if not frame_path:
            placeholder = os.path.join("resource", "nocam.png")
            if not os.path.exists(placeholder):
                placeholder = os.path.join("resource", "noconnect.png")
        else:
            current_mtime = os.path.getmtime(frame_path)
            if current_mtime <= last_mtime:
                time.sleep(0.1)
                continue
            last_mtime = current_mtime

        # Читаем изображение
        try:
            with open(frame_path or placeholder, "rb") as f:
                frame_data = f.read()
            yield (b'--frame\r\n'
                   b'Content-Type: image/png\r\n\r\n' + frame_data + b'\r\n')
        except Exception as e:
            print(f"[WEB] Ошибка чтения кадра cam{cam_id}: {e}")

        time.sleep(REFRESH_INTERVAL)

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

@app.route('/cam<int:cam_id>')
def cam_page(cam_id):
    if cam_id < 1 or cam_id > 9:
        return "Камера не найдена", 404
    return render_template_string(HTML_TEMPLATE, cam_id=cam_id)

@app.route('/video_feed/<int:cam_id>')
def video_feed(cam_id):
    if cam_id < 1 or cam_id > 9:
        return "Камера не найдена", 404
    return Response(generate_frames(cam_id),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

# ----------------------------------------------------------------------
# Запуск сервера в отдельном потоке
# ----------------------------------------------------------------------
def run_server():
    """Запуск Flask-сервера в фоновом потоке"""
    print(f"[WEB] Веб-сервер запущен: http://localhost:{PORT}")
    print(f"[WEB] Доступны страницы: /cam1 ... /cam9")
    app.run(host=HOST, port=PORT, debug=DEBUG, threaded=True, use_reloader=False)

def start_web_server():
    """Запуск сервера в отдельном потоке"""
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    return thread

# ----------------------------------------------------------------------
# Автозапуск при импорте (опционально)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    start_web_server()
    # Бесконечный цикл, чтобы процесс не завершился
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[WEB] Сервер остановлен.")