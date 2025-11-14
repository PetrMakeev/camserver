import os
import sys
import time
import psutil
import logging
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import shutil

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
# Логи
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
        maxBytes=5*1024*1024,
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
print("Приложение запущено. Проверяем chromedriver...")

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
        logging.warning(f"Не удалось ротировать лог: {e}")
        replace_log_handler()

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
# Блокировка дублирующего запуска
# ----------------------------------------------------------------------
if sys.platform.startswith('win'):
    try:
        import win32event
        import win32api
        from winerror import ERROR_ALREADY_EXISTS
        mutex = win32event.CreateMutex(None, False, "Global\\CaptureApp_SingleInstance_Mutex")
        if win32api.GetLastError() == ERROR_ALREADY_EXISTS:
            print("Ошибка: Приложение уже запущено!")
            sys.exit(1)
    except ImportError:
        print("pywin32 не установлен. Пропуск проверки дублирования.")

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
                if img.getpixel((x, y))[:3] != (0, 0,199):
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
# Проверка chromedriver
# ----------------------------------------------------------------------
def find_chromedriver():
    possible_paths = [
        "chromedriver.exe",
        os.path.join("chromedriver", "chromedriver.exe"),
        os.path.join(sys._MEIPASS, "chromedriver.exe") if getattr(sys, 'frozen', False) else None,
    ]
    for path in possible_paths:
        if path and os.path.isfile(path):
            return path
    return None

chromedriver_path = find_chromedriver()
if not chromedriver_path:
    error_msg = "ОШИБКА: chromedriver.exe НЕ НАЙДЕН!\n" \
                "Поместите chromedriver.exe в папку со скриптом или в подпапку 'chromedriver/'.\n" \
                "Скачать: https://chromedriver.chromium.org/downloads (версия под ваш Chrome!)"
    logging.critical(error_msg)
    print(error_msg)
    sys.exit(1)
else:
    logging.info(f"chromedriver найден: {chromedriver_path}")
    print(f"chromedriver найден: {chromedriver_path}")

# ----------------------------------------------------------------------
# Проверка заглушек
# ----------------------------------------------------------------------
nocam_path = resource_path(os.path.join("resource", "nocam.png"))
noconnect_path = resource_path(os.path.join("resource", "noconnect.png"))

if not os.path.exists(nocam_path):
    logging.warning("resource/nocam.png не найден — будет пропущен")
if not os.path.exists(noconnect_path):
    logging.warning("resource/noconnect.png не найден — будет пропущен")

# ----------------------------------------------------------------------
# Конфиг
# ----------------------------------------------------------------------
class ConfigManager:
    DEFAULT_URLS = [None] * 9

    def __init__(self, filename='url.yaml'):
        self.filename = filename
        self.yaml = YAML()
        self.urls = self.DEFAULT_URLS.copy()
        self._load()

    def _load(self):
        if not os.path.exists(self.filename):
            logging.warning(f"Файл {self.filename} не найден, используются пустые URL")
            print(f"ВНИМАНИЕ: {self.filename} не найден!")
            return

        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                loaded = self.yaml.load(f) or {}
            if 'urls' in loaded and isinstance(loaded['urls'], list):
                urls = loaded['urls']
                self.urls = (urls + [None] * 9)[:9]
                logging.info(f"Загружено {len([u for u in self.urls if u])} камер из {self.filename}")
                print(f"Загружено {len([u for u in self.urls if u])} камер")
            else:
                logging.warning("Неверный формат url.yaml")
                print("ОШИБКА: Неверный формат url.yaml")
        except Exception as e:
            logging.error(f"Ошибка загрузки url.yaml: {e}")
            print(f"Ошибка загрузки url.yaml: {e}")

# ----------------------------------------------------------------------
# Драйвер
# ----------------------------------------------------------------------
class BrowserDriver:
    def __init__(self, url, cam_index):
        self.url = url
        self.cam_index = cam_index
        self.driver = None
        self.iframe_element = None
        self._init_driver()

    def _init_driver(self):
        if not self.url:
            return

        try:
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-infobars")
            chrome_options.add_argument("--disable-notifications")
            chrome_options.add_experimental_option("useAutomationExtension", False)
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])

            service = Service(executable_path=chromedriver_path)
            self.driver = webdriver.Chrome(service=service, options=chrome_options)
            logging.info(f"Драйвер запущен для cam{self.cam_index}")
            self._init_page()
        except Exception as e:
            logging.error(f"Не удалось запустить драйвер для cam{self.cam_index}: {e}")
            self.driver = None

    def _init_page(self):
        try:
            self.driver.get(self.url)
            WebDriverWait(self.driver, 25).until(EC.presence_of_element_located((By.ID, "ModalBodyPlayer")))
            self.iframe_element = WebDriverWait(self.driver, 25).until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
            logging.info(f"Страница загружена для cam{self.cam_index}")
        except Exception as e:
            logging.error(f"Не загрузилась страница для cam{self.cam_index}: {e}")
            self.driver = None

    def reload_via_url(self):
        if not self.driver:
            self._init_driver()
            return False
        try:
            logging.info(f"Перезагрузка страницы для cam{self.cam_index}")
            self.driver.get(self.url)
            self.driver.refresh()
            time.sleep(2)
            WebDriverWait(self.driver, 30).until(EC.presence_of_element_located((By.ID, "ModalBodyPlayer")))
            self.iframe_element = WebDriverWait(self.driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
            return True
        except Exception as e:
            logging.error(f"Ошибка перезагрузки для cam{self.cam_index}: {e}")
            self.restart()
            return False

    def restart(self):
        if self.driver:
            try: self.driver.quit()
            except: pass
        time.sleep(3)
        self._init_driver()

    def get_iframe_size(self):
        if not self.driver or not self.iframe_element:
            return None
        try:
            return self.driver.execute_script("return arguments[0].getBoundingClientRect()", self.iframe_element)
        except:
            return None

    def capture_frame(self, file_path):
        if not self.driver:
            return False
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
            logging.warning(f"Ошибка захвата кадра для cam{self.cam_index}: {e}")
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
    def __init__(self, driver, cam_index):
        self.driver = driver
        self.cam_index = cam_index
        self.folder = os.path.join("capture", f"cam{cam_index}")
        os.makedirs(self.folder, exist_ok=True)

    def capture(self):
        now = datetime.now()
        date_str = now.strftime("%Y%m%d")
        time_str = now.strftime("%H-%M-%S")
        filename = f"capt-{date_str}_{time_str}.png"
        file_path = os.path.join(self.folder, filename)

        # Нет URL — заглушка
        if not self.driver.url:
            if os.path.exists(nocam_path):
                shutil.copy(nocam_path, file_path)
                logging.info(f"nocam.png → cam{self.cam_index}")
            return True

        # Пытаемся захватить
        try:
            if not self.driver.driver:
                self.driver.restart()

            size = self.driver.get_iframe_size()
            if not size or size['width'] < 1 or size['height'] < 1:
                self.driver.reload_via_url()
                return self._save_noconnect(file_path)

            if not self.driver.capture_frame(file_path):
                self.driver.reload_via_url()
                return self._save_noconnect(file_path)

            if is_image_black(Image.open(file_path)):
                os.remove(file_path)
                self.driver.reload_via_url()
                return self._save_noconnect(file_path)

            with Image.open(file_path) as img:
                w, h = img.size
                if w < 132:
                    os.remove(file_path)
                    self.driver.reload_via_url()
                    return self._save_noconnect(file_path)
                img.crop((66, 0, w-66, h)).save(file_path, quality=95)

            if os.path.getsize(file_path) / 1024 < 100:
                os.remove(file_path)
                self.driver.reload_via_url()
                return self._save_noconnect(file_path)

            logging.info(f"Успешно сохранено: cam{self.cam_index} → {filename}")
            self._limit_frames()
            return True

        except Exception as e:
            try: os.remove(file_path)
            except: pass
            logging.error(f"Исключение при захвате cam{self.cam_index}: {e}")
            self.driver.restart()
            return self._save_noconnect(file_path)

    def _save_noconnect(self, file_path):
        if os.path.exists(noconnect_path):
            shutil.copy(noconnect_path, file_path)
            logging.info(f"noconnect.png → cam{self.cam_index}")
            self._limit_frames()
            return True
        return False

    def _limit_frames(self):
        frames = sorted(Path(self.folder).glob("capt-*.png"), key=lambda x: x.stat().st_mtime, reverse=True)
        for old in frames[10:]:
            try:
                old.unlink()
                logging.info(f"Удалён старый кадр: {old.name}")
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
        time.sleep(1)

# ----------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------
if __name__ == "__main__":
    cleanup_processes()
    config = ConfigManager()

    print("Запуск 9 потоков захвата...")
    threads = []
    for i in range(9):
        url = config.urls[i]
        t = threading.Thread(target=capture_thread, args=(i+1, url), daemon=True)
        t.start()
        threads.append(t)
        print(f"  → cam{i+1}: {'[URL]' if url else '[NO CAM]'}")

    last_log_date = datetime.now().strftime("%Y%m%d")
    try:
        while True:
            now = datetime.now()
            today_str = now.strftime("%Y%m%d")
            if last_log_date != today_str:
                rotate_log_if_needed()
                last_log_date = today_str
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nОстановка...")
        cleanup_processes()
        sys.exit(0)