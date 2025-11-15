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

from ruamel.yaml import YAML

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from PIL import Image

import urllib3
from selenium.webdriver.remote.remote_connection import LOGGER as SELENIUM_LOGGER

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("selenium").setLevel(logging.ERROR)
SELENIUM_LOGGER.setLevel(logging.ERROR)

# ----------------------------------------------------------------------
# === КОНФИГУРАЦИЯ ===
# ----------------------------------------------------------------------
CAPTURE_INTERVAL = 1  # Интервал захвата кадров (секунды)

# ----------------------------------------------------------------------
# Логи (ротация по суткам)
# ----------------------------------------------------------------------
LOG_DIR = "."
LOG_BASE = "capture"
LOG_EXT = ".log"
MAX_LOG_DAYS = 5

def get_current_log_path():
    return os.path.join(LOG_DIR, f"{LOG_BASE}{LOG_EXT}")

def get_dated_log_path(date_str):
    return os.path.join(LOG_DIR, f"{LOG_BASE}_{date_str}{LOG_EXT}")

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

def replace_log_handler():
    root = logging.getLogger()
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
    root.addHandler(create_new_handler())
    root.setLevel(logging.INFO)

replace_log_handler()
logging.info("=== КОНСОЛЬНОЕ ПРИЛОЖЕНИЕ ЗАПУЩЕНО ===")

def rotate_log_if_needed():
    current_log = get_current_log_path()
    if not os.path.exists(current_log):
        return

    yesterday = datetime.now() - timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y%m%d")
    dated_log = get_dated_log_path(yesterday_str)
    if os.path.exists(dated_log):
        return

    try:
        root = logging.getLogger()
        for h in root.handlers[:]:
            h.close()
            root.removeHandler(h)
        os.rename(current_log, dated_log)
        logging.info(f"Лог переименован: {current_log} → {dated_log}")
        replace_log_handler()
    except Exception as e:
        try:
            replace_log_handler()
        except:
            pass
        logging.warning(f"Не удалось ротировать лог: {e}")

    cutoff = datetime.now() - timedelta(days=MAX_LOG_DAYS)
    for file in Path(LOG_DIR).glob(f"{LOG_BASE}_*{LOG_EXT}"):
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
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# ----------------------------------------------------------------------
# Конфиг
# ----------------------------------------------------------------------
class ConfigManager:
    DEFAULT_URLS = [None] * 9
    def __init__(self, filename='url.yaml'):
        self.filename = filename
        self.yaml = YAML()
        self.yaml.preserve_quotes = False
        self.urls = self.DEFAULT_URLS.copy()
        self._load()

    def _load(self):
        if not os.path.exists(self.filename):
            logging.warning(f"Файл {self.filename} не найден")
            return
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                loaded = self.yaml.load(f) or {}
            if 'urls' in loaded and isinstance(loaded['urls'], list):
                self.urls = loaded['urls'][:9] + [None] * (9 - len(loaded['urls']))
        except Exception as e:
            logging.error(f"Ошибка загрузки url.yaml: {e}")

# ----------------------------------------------------------------------
# Драйвер
# ----------------------------------------------------------------------
class BrowserDriver:
    def __init__(self, url, cam_index):
        self.url = url
        self.cam_index = cam_index
        self.driver = None
        self.iframe_element = None
        if self.url:
            self._setup_driver()
            self._init_page()

    def _setup_driver(self):
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chromedriver_path = os.path.join(sys._MEIPASS, "chromedriver.exe") if getattr(sys, 'frozen', False) else "chromedriver.exe"
        service = Service(executable_path=chromedriver_path)
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
        self.folder = os.path.join("capture", f"cam{cam_index}")
        os.makedirs(self.folder, exist_ok=True)
        self.current_path = os.path.join(self.folder, self.CURRENT_FILE)
        self.temp_path = os.path.join(self.folder, self.TEMP_FILE)

    def capture(self):
        if not self.driver.url:
            src = resource_path(os.path.join("resource", "nocam.png"))
            if os.path.exists(src):
                shutil.copy(src, self.current_path)
                logging.info(f"nocam.png → cam{self.cam_index}")
            return True

        try:
            size = self.driver.get_iframe_size()
            if not size or size['width'] < 1:
                if self.driver.reload_via_url():
                    time.sleep(1)
                return self._save_noconnect()

            if not self.driver.capture_frame(self.temp_path):
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

            if os.path.getsize(self.temp_path) / 1024 < 100:
                self._safe_remove(self.temp_path)
                if self.driver.reload_via_url():
                    time.sleep(1)
                return self._save_noconnect()

            if os.path.exists(self.current_path):
                os.replace(self.temp_path, self.current_path)
            else:
                os.rename(self.temp_path, self.current_path)
            logging.info(f"Кадр обновлён: cam{self.cam_index}")
            return True

        except Exception as e:
            self._safe_remove(self.temp_path)
            logging.error(f"Ошибка захвата cam{self.cam_index}: {e}")
            if self.driver.reload_via_url():
                time.sleep(1)
            return self._save_noconnect()

    def _save_noconnect(self):
        src = resource_path(os.path.join("resource", "noconnect.png"))
        if os.path.exists(src):
            shutil.copy(src, self.current_path)
            logging.info(f"noconnect.png → cam{self.cam_index}")
            return True
        return False

    def _safe_remove(self, path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except:
            pass

# ----------------------------------------------------------------------
# Поток захвата
# ----------------------------------------------------------------------
def capture_thread(cam_index, url):
    driver = BrowserDriver(url, cam_index)
    capture = FrameCapture(driver, cam_index)
    while True:
        capture.capture()
        time.sleep(CAPTURE_INTERVAL)

# ----------------------------------------------------------------------
# Веб-сервер
# ----------------------------------------------------------------------
try:
    from web_server import start_web_server
except ImportError:
    logging.error("web_server.py не найден")
    start_web_server = lambda: None

# ----------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------
if __name__ == "__main__":
    cleanup_processes()
    exit_if_port_busy()

    config = ConfigManager()
    threads = []
    for i in range(9):
        url = config.urls[i] if i < len(config.urls) else None
        t = threading.Thread(target=capture_thread, args=(i+1, url), daemon=True)
        t.start()
        threads.append(t)

    web_thread = start_web_server()

    last_log_date = datetime.now().strftime("%Y%m%d")
    NOCAM_PATH = resource_path(os.path.join("resource", "nocam.png"))
    if not os.path.exists(NOCAM_PATH):
        logging.warning("Заглушка nocam.png не найдена — завершение без замены кадров")

    try:
        print(f"\nПриложение запущено.")
        print(f"Интервал захвата: {CAPTURE_INTERVAL} сек")
        print(f"Веб: http://localhost:5000")
        print(f"VLC: http://localhost:5000/stream/cam1 ... /stream/cam9")
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

        # === 1. Сначала копируем заглушку + обновляем mtime ===
        if os.path.exists(NOCAM_PATH):
            for cam_id in range(1, 10):
                folder = os.path.join("capture", f"cam{cam_id}")
                target = os.path.join(folder, "current.png")
                try:
                    if os.path.exists(folder):
                        shutil.copy2(NOCAM_PATH, target)  # Сохраняет метаданные
                        os.utime(target, None)  # <<< ГАРАНТИЯ изменения mtime
                        logging.info(f"Заглушка → cam{cam_id}")
                        print(f"  cam{cam_id} → заглушка")
                except Exception as e:
                    logging.error(f"Ошибка копирования в cam{cam_id}: {e}")

        # === 2. Ждём, чтобы MJPEG отправил заглушку ===
        print("  Ожидание 2 сек для доставки заглушки клиентам...")
        time.sleep(2)

        # === 3. Только потом шатдауним веб-сервер ===
        try:
            print("  Отправка команды завершения веб-серверу...")
            requests.get("http://127.0.0.1:5000/shutdown", timeout=3)
        except Exception as e:
            logging.warning(f"Не удалось завершить веб-сервер: {e}")

        # === 4. Финальная пауза ===
        print("  Ожидание 3 сек для завершения потоков...")
        time.sleep(3)

        print("  Удаление Chrome/Driver...")
        cleanup_processes()

        print("Приложение завершено.\n")
        sys.exit(0)