# main.py
import os
import sys
import time
import psutil
import logging
import requests
import shutil
import socket
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
from queue import Queue

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from PIL import Image
import io

import urllib3
from selenium.webdriver.remote.remote_connection import LOGGER as SELENIUM_LOGGER

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("selenium").setLevel(logging.ERROR)
SELENIUM_LOGGER.setLevel(logging.ERROR)

# ----------------------------------------------------------------------
# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ API ===
# ----------------------------------------------------------------------
CAM_URLS = [None] * 9
URL_UPDATE_QUEUE = Queue()

# ----------------------------------------------------------------------
# === КОНФИГУРАЦИЯ ===
# ----------------------------------------------------------------------
CAPTURE_INTERVAL = 1

# ----------------------------------------------------------------------
# Логи (ротация по суткам)
# ----------------------------------------------------------------------
LOG_DIR = Path(".")
LOG_BASE = "capture"
LOG_EXT = ".log"
MAX_LOG_DAYS = 5

def get_current_log_path():
    return LOG_DIR / f"{LOG_BASE}{LOG_EXT}"

def get_dated_log_path(date_str):
    return LOG_DIR / f"{LOG_BASE}_{date_str}{LOG_EXT}"

def create_new_handler():
    handler = RotatingFileHandler(
        get_current_log_path(),
        maxBytes=5 * 1024 * 1024,
        backupCount=1,
        delay=True,
        encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    return handler

def setup_logging():
    root = logging.getLogger()
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
    root.addHandler(create_new_handler())
    root.setLevel(logging.INFO)

# === ОДИН РАЗ ПРИ СТАРТЕ ===
setup_logging()
logging.info("=== КОНСОЛЬНОЕ ПРИЛОЖЕНИЕ ЗАПУЩЕНО ===")

def rotate_log_if_needed():
    current_log = get_current_log_path()
    if not current_log.exists():
        return

    yesterday = datetime.now() - timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y%m%d")
    dated_log = get_dated_log_path(yesterday_str)
    if dated_log.exists():
        return

    try:
        root = logging.getLogger()
        for h in root.handlers[:]:
            h.close()
            root.removeHandler(h)
        current_log.rename(dated_log)
        logging.info(f"Лог переименован: {current_log} → {dated_log}")
        setup_logging()
    except Exception as e:
        try:
            setup_logging()
        except:
            pass
        logging.warning(f"Не удалось ротировать лог: {e}")

    cutoff = datetime.now() - timedelta(days=MAX_LOG_DAYS)
    for file in LOG_DIR.glob(f"{LOG_BASE}_*{LOG_EXT}"):
        try:
            file_date_str = file.stem.split("_")[-1]
            file_date = datetime.strptime(file_date_str, "%Y%m%d")
            if file_date < cutoff:
                file.unlink()
                logging.info(f"Удалён старый лог: {file.name}")
        except Exception as e:
            logging.warning(f"Ошибка при удалении старого лога {file.name}: {e}")

# ----------------------------------------------------------------------
# Проверка порта 5000
# ----------------------------------------------------------------------
def check_port_free(port=5000, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False

def exit_if_port_busy():
    if not check_port_free(5000):
        print("Ошибка: порт 5000 уже занят.")
        print("Освободите порт или измените PORT в web_server.py")
        logging.critical("Порт 5000 занят — приложение завершено.")
        sys.exit(1)

# ----------------------------------------------------------------------
# Утилиты
# ----------------------------------------------------------------------
def cleanup_processes():
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'].lower() in ['chromedriver.exe', 'chrome.exe']:
                proc.kill()
                logging.info(f"Убит: {proc.info['name']} (PID: {proc.info['pid']})")
        except Exception as e:
            logging.warning(f"Не удалось убить процесс: {e}")

def is_image_black(img):
    try:
        w, h = img.size
        for x in range(0, w, 10):
            for y in range(0, h, 10):
                if img.getpixel((x, y))[:3] != (0, 0, 0):
                    return False
        return True
    except:
        return False

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = Path.cwd()
    return Path(base_path) / relative_path

# ----------------------------------------------------------------------
# Кэширование nocam.png
# ----------------------------------------------------------------------
NOCAM_IMAGE_BYTES = None
NOCAM_PATH = resource_path(Path("resource") / "nocam.png")

def load_nocam_image():
    global NOCAM_IMAGE_BYTES
    if NOCAM_PATH.exists():
        try:
            with NOCAM_PATH.open('rb') as f:
                NOCAM_IMAGE_BYTES = f.read()
            logging.info("nocam.png загружен в память")
        except Exception as e:
            logging.error(f"Не удалось загрузить nocam.png: {e}")
    else:
        logging.warning("Заглушка nocam.png не найдена")

# ----------------------------------------------------------------------
# Драйвер
# ----------------------------------------------------------------------
class BrowserDriver:
    def __init__(self, url, cam_index):
        self.url = url or "about:blank"
        self.cam_index = cam_index
        self.driver = None
        self.iframe_element = None
        if self.url != "about:blank":
            self._setup_driver()
            self._init_page()

    def _setup_driver(self):
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chromedriver_path = resource_path("chromedriver.exe")
        service = Service(executable_path=str(chromedriver_path))
        self.driver = webdriver.Chrome(service=service, options=chrome_options)

    def _init_page(self):
        try:
            self.driver.get(self.url)
            WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.ID, "ModalBodyPlayer")))
            self.iframe_element = WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
        except Exception as e:
            logging.error(f"Не загрузилась страница для cam{self.cam_index}: {e}")
            self.driver = None

    def reload_via_url(self):
        try:
            logging.info(f"Перезагрузка cam{self.cam_index}")
            self.driver.get(self.url)
            self.driver.refresh()
            time.sleep(1)
            WebDriverWait(self.driver, 25).until(EC.presence_of_element_located((By.ID, "ModalBodyPlayer")))
            self.iframe_element = WebDriverWait(self.driver, 25).until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
            return "about:blank" not in self.iframe_element.get_attribute("src")
        except Exception as e:
            logging.error(f"Ошибка перезагрузки cam{self.cam_index}: {e}")
            return False

    def get_iframe_size(self):
        try:
            return self.driver.execute_script("return arguments[0].getBoundingClientRect()", self.iframe_element)
        except Exception as e:
            logging.warning(f"get_iframe_size error cam{self.cam_index}: {e}")
            return None

    def capture_frame(self, file_path):
        try:
            self.driver.switch_to.frame(self.iframe_element)
            try:
                video = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, "video")))
                video.screenshot(file_path)
            except:
                self.driver.switch_to.default_content()
                self.iframe_element.screenshot(file_path)
            else:
                self.driver.switch_to.default_content()
            return True
        except Exception as e:
            logging.warning(f"capture_frame error cam{self.cam_index}: {e}")
            return False

    def quit(self):
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass

# ----------------------------------------------------------------------
# Захват
# ----------------------------------------------------------------------
class FrameCapture:
    CURRENT_FILE = "current.png"
    TEMP_FILE = "temp_capture.png"

    def __init__(self, driver, cam_index):
        self.driver = driver
        self.cam_index = cam_index
        self.folder = Path("capture") / f"cam{cam_index}"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.current_path = self.folder / self.CURRENT_FILE
        self.temp_path = self.folder / self.TEMP_FILE
        self.nocam_logged = False  # ← ОДИН РАЗ ПРИ СТАРТЕ

    def capture(self):
        if not self.driver or self.driver.url == "about:blank":
            if not self.nocam_logged:
                logging.info(f"nocam.png -> cam{self.cam_index} (старт)")
                self.nocam_logged = True
            return self._save_noconnect()

        try:
            size = self.driver.get_iframe_size()
            if not size or size['width'] < 1:
                if self.driver.reload_via_url():
                    time.sleep(1)
                return self._save_noconnect()

            if not self.driver.capture_frame(str(self.temp_path)):
                if self.driver.reload_via_url():
                    time.sleep(1)
                return self._save_noconnect()

            if is_image_black(Image.open(self.temp_path)):
                self._safe_remove(self.temp_path)
                if self.driver.reload_via_url():
                    time.sleep(1)
                return self._save_noconnect()

            with Image.open(self.temp_path) as img:
                w, h = img.size
                if w < 132:
                    self._safe_remove(self.temp_path)
                    if self.driver.reload_via_url():
                        time.sleep(1)
                    return self._save_noconnect()
                img.crop((66, 0, w-66, h)).save(self.temp_path, format='PNG', quality=95)

            if self.temp_path.stat().st_size / 1024 < 100:
                self._safe_remove(self.temp_path)
                if self.driver.reload_via_url():
                    time.sleep(1)
                return self._save_noconnect()

            if self.current_path.exists():
                self.temp_path.replace(self.current_path)
            else:
                self.temp_path.rename(self.current_path)
            return True

        except Exception as e:
            self._safe_remove(self.temp_path)
            logging.error(f"Ошибка захвата cam{self.cam_index}: {e}")
            if self.driver and self.driver.reload_via_url():
                time.sleep(1)
            return self._save_noconnect()

    def _save_noconnect(self):
        if NOCAM_IMAGE_BYTES:
            try:
                self.current_path.write_bytes(NOCAM_IMAGE_BYTES)
                os.utime(self.current_path, None)
                # НЕ ЛОГИРУЕМ КАЖДЫЙ ТИК
                return True
            except Exception as e:
                logging.error(f"Ошибка записи nocam.png для cam{self.cam_index}: {e}")
        return False

    def _safe_remove(self, path):
        try:
            if path.exists():
                path.unlink()
        except:
            pass

# ----------------------------------------------------------------------
# Поток захвата
# ----------------------------------------------------------------------
def capture_thread(cam_index, initial_url):
    url = initial_url
    driver = None
    capture = None

    def restart_driver(new_url):
        nonlocal driver, capture

        if driver:
            try: driver.quit()
            except: pass
            driver = None
        if capture:
            capture._safe_remove(capture.temp_path)

        time.sleep(1.5)

        if new_url and new_url != "about:blank":
            driver = BrowserDriver(new_url, cam_index)
            capture = FrameCapture(driver, cam_index)
        else:
            driver = BrowserDriver("about:blank", cam_index)
            capture = FrameCapture(driver, cam_index)
            # Логируем только при старте
            capture._save_noconnect()

    restart_driver(url)

    while True:
        updated = False
        try:
            while True:
                event = URL_UPDATE_QUEUE.get_nowait()
                if isinstance(event, tuple) and len(event) == 2:
                    cam_id, new_url = event
                    if cam_id == cam_index:
                        logging.info(f"API: cam{cam_index} → {new_url or 'отключена'}")
                        if new_url != url:
                            url = new_url
                            logging.info(f"Перезапуск cam{cam_index} → {url or 'отключена'}")
                            restart_driver(url)
                        updated = True
                        break
                    else:
                        URL_UPDATE_QUEUE.put(event)
        except:
            pass

        if updated:
            time.sleep(CAPTURE_INTERVAL)
            continue

        if capture:
            capture.capture()
        time.sleep(CAPTURE_INTERVAL)

# ----------------------------------------------------------------------
# Веб-сервер (без логов об импорте)
# ----------------------------------------------------------------------
try:
    from web_server import start_web_server, set_update_queue
    WEB_SERVER_AVAILABLE = True
except Exception:  # ← НЕ ЛОГИРУЕМ ОШИБКУ
    start_web_server = lambda: None
    set_update_queue = lambda q: None
    WEB_SERVER_AVAILABLE = False

# ----------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------
if __name__ == "__main__":
    cleanup_processes()
    exit_if_port_busy()

    load_nocam_image()

    threads = []
    for i in range(9):
        t = threading.Thread(target=capture_thread, args=(i+1, None), daemon=True)
        t.start()
        threads.append(t)

    web_thread = start_web_server()

    if WEB_SERVER_AVAILABLE:
        try:
            set_update_queue(URL_UPDATE_QUEUE)
            logging.info("Очередь обновлений URL передана в web_server")
        except Exception as e:
            logging.error(f"Не удалось передать очередь: {e}")
    else:
        logging.warning("web_server.py недоступен — API и потоки отключены")

    last_log_date = datetime.now().strftime("%Y%m%d")

    try:
        print(f"\nПриложение запущено.")
        print(f"Интервал захвата: {CAPTURE_INTERVAL} сек")
        print(f"Веб: http://localhost:5000")
        print(f"VLC: http://localhost:5000/stream/cam1 ... /stream/cam9")
        print(f"API: POST /api/set_urls → сменить URL")
        print(f"API: GET /api/status → статус камер")
        print(f"Для остановки: Ctrl+C\n")

        while True:
            now = datetime.now()
            today_str = now.strftime("%Y%m%d")
            if last_log_date != today_str:
                rotate_log_if_needed()
                last_log_date = today_str
            time.sleep(60)

    except KeyboardInterrupt:
        print("\n\nПолучен сигнал завершения (Ctrl+C)...")
        logging.info("Инициация graceful shutdown...")

        # === 1. КОПИРУЕМ ЗАГЛУШКУ ВО ВСЕ current.png ===
        if NOCAM_IMAGE_BYTES:
            for cam_id in range(1, 10):
                folder = Path("capture") / f"cam{cam_id}"
                target = folder / "current.png"
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(NOCAM_IMAGE_BYTES)
                    os.utime(target, None)  # ← mtime меняется → MJPEG увидит
                    logging.info(f"Заглушка -> cam{cam_id} (выход)")
                    print(f"  cam{cam_id} → заглушка")
                except Exception as e:
                    logging.error(f"Ошибка записи в cam{cam_id}: {e}")

        # === 2. ДАЁМ ВРЕМЯ MJPEG ОТПРАВИТЬ КАДР ===
        print("  Ожидание 4 сек для доставки заглушки клиентам...")
        time.sleep(4)  # ← УВЕЛИЧЕНО до 4 сек

        # === 3. ЗАВЕРШАЕМ СЕРВЕР ===
        try:
            print("  Отправка команды завершения веб-серверу...")
            requests.get("http://127.0.0.1:5000/shutdown", timeout=5)
        except Exception as e:
            logging.warning(f"Не удалось завершить сервер: {e}")

        time.sleep(2)
        cleanup_processes()
        print("Приложение завершено.\n")
        sys.exit(0)