"""Pixiv auto-stamp desktop application.

The configuration file is intentionally kept beside the executable so that
credentials are not embedded in the distributable program.
"""

from __future__ import annotations

import ctypes
import json
import queue
import sys
import threading
import time
import traceback
import winreg
from dataclasses import dataclass, asdict
from ctypes import wintypes
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests
import tkinter as tk
import pystray
from PIL import Image, ImageTk
from tkinter import messagebox, ttk


APP_NAME = "Pixiv 自動貼圖"
POLL_INTERVAL_SECONDS = 30
MAX_SEEN_IDS = 300
AUTOSTART_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "PixivAutoStamp"
SINGLE_INSTANCE_MUTEX_NAME = r"Local\PixivAutoStampExecute"
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9
_single_instance_mutex = None


def app_directory() -> Path:
    """Return the directory containing the executable (or this script)."""
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def resource_path(filename: str) -> Path:
    """Return a bundled resource path when running from a PyInstaller exe."""
    base_directory = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_directory / filename


def show_existing_window() -> None:
    """Restore a running instance, including one hidden in the system tray."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    for _ in range(10):
        window_handle = user32.FindWindowW(None, APP_NAME)
        if window_handle:
            user32.ShowWindow(window_handle, SW_RESTORE)
            user32.SetForegroundWindow(window_handle)
            return
        time.sleep(0.1)


def acquire_single_instance() -> bool:
    """Return False when another instance is already running."""
    global _single_instance_mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    mutex_handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    if not mutex_handle:
        # Do not block the app if the mutex API is unexpectedly unavailable.
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        show_existing_window()
        kernel32.CloseHandle(mutex_handle)
        return False
    _single_instance_mutex = mutex_handle
    return True


CONFIG_PATH = app_directory() / "config.json"
STATE_PATH = app_directory() / "seen_id.json"
APP_ICON_PATH = resource_path("pixiv_autostamp_icon.png")


@dataclass
class Settings:
    keyword: str = "御稜ナグサ"
    php_sessid: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notes: str = ""
    excluded_authors: str = ""
    auto_start_on_login: bool = False

    @classmethod
    def load(cls) -> "Settings":
        if not CONFIG_PATH.exists():
            settings = cls()
            settings.save()
            return settings
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return cls(**{key: raw.get(key, value) for key, value in asdict(cls()).items()})
        except (OSError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def excluded_author_set(self) -> set[str]:
        """Return non-empty author names entered one per line."""
        return {name.strip() for name in self.excluded_authors.splitlines() if name.strip()}

    def keywords(self) -> list[str]:
        """Return non-empty search keywords entered one per line."""
        return list(dict.fromkeys(word.strip() for word in self.keyword.splitlines() if word.strip()))


def configure_autostart(enabled: bool) -> None:
    """Add or remove this application from the current user's login startup."""
    if getattr(sys, "frozen", False):
        command = f'"{Path(sys.executable).resolve()}"'
    else:
        command = f'"{Path(sys.executable).resolve()}" "{Path(__file__).resolve()}"'

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, AUTOSTART_REGISTRY_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass


class PixivWorker:
    def __init__(self, settings: Settings, log: Callable[[str], None], stop_event: threading.Event):
        self.settings = settings
        self.log = log
        self.stop_event = stop_event
        self.driver = None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.pixiv.net/",
            "Accept": "application/json",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        })
        self.session.cookies.update({"PHPSESSID": settings.php_sessid})

    def run(self) -> None:
        self.log("服務已啟動，將每 30 秒檢查一次新作品。")
        try:
            while not self.stop_event.is_set():
                self.check_once()
                if self.stop_event.wait(POLL_INTERVAL_SECONDS):
                    break
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 401:
                self.log(
                    "Pixiv 驗證失敗（401）：PHPSESSID 已失效或不正確。"
                    "請重新從已登入 Pixiv 的瀏覽器取得 PHPSESSID，貼到設定欄位後按「儲存設定」，再重新啟動服務。"
                )
            else:
                self.log(f"Pixiv 網路請求失敗（HTTP {status_code or '未知'}）：{exc}")
                self.log(traceback.format_exc())
        except Exception as exc:
            self.log(f"執行發生未預期錯誤：{exc}")
            self.log(traceback.format_exc())
        finally:
            self.close_driver()
            self.log("服務已停止。")

    def check_once(self) -> None:
        if self.stop_event.is_set():
            return
        seen = self.load_seen()
        found_new_work = False
        excluded_authors = self.settings.excluded_author_set()
        for keyword in self.settings.keywords():
            if self.stop_event.is_set():
                return
            url = "https://www.pixiv.net/ajax/search/artworks/{}?word={}&mode=tag&order=date_d&p=1".format(
                quote(keyword), quote(keyword)
            )
            self.log(f"正在查詢關鍵字「{keyword}」…")
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            works = response.json()["body"]["illustManga"]["data"]
            self.log(f"「{keyword}」找到 {len(works)} 個作品。")
            new_works = [work for work in works[:10] if str(work["id"]) not in seen]
            if not new_works:
                continue

            found_new_work = True
            self.log(f"「{keyword}」偵測到 {len(new_works)} 個新作品。")
            for work in reversed(new_works):
                if self.stop_event.is_set():
                    return
                work_id, title, author = str(work["id"]), work["title"], work["userName"]
                work_url = f"https://www.pixiv.net/artworks/{work_id}"
                self.notify(f"【Pixiv 新作品】\n\n關鍵字: {keyword}\n標題: {title}\n作者: {author}\n\n{work_url}")
                if self.comments_disabled(work_id):
                    self.log(f"略過已關閉留言的作品：{title}")
                    seen[work_id] = None
                    continue
                if author in excluded_authors:
                    self.log(f"略過排除作者的作品：{title}（{author}）")
                    seen[work_id] = None
                    continue
                self.log(f"正在對作品貼圖：{title}")
                if self.click_stamp(work_id):
                    seen[work_id] = None
                    self.notify(f"✅ 已點擊貼圖\n\n作品: {title}")
                    self.log("貼圖完成。")
                else:
                    self.notify(f"❌ 點擊貼圖失敗\n\n作品: {title}")
                    self.log("貼圖失敗，保留此作品供下次重試。")
        if not found_new_work:
            self.log("所有關鍵字均沒有新作品。")
        self.save_seen(seen)
        # Chrome is created lazily only when a stamp must be clicked. Close it
        # once this polling round has handled every new work.
        self.close_driver()

    def comments_disabled(self, work_id: str) -> bool:
        """Return whether Pixiv has disabled comments/stamps for this work."""
        try:
            response = self.session.get(
                f"https://www.pixiv.net/ajax/illust/{work_id}", timeout=15
            )
            response.raise_for_status()
            return bool(response.json().get("body", {}).get("commentOff"))
        except (requests.RequestException, ValueError) as exc:
            self.log(f"無法確認留言設定，將照常嘗試貼圖：{exc}")
            return False

    def notify(self, text: str) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            self.log("未設定 Telegram，略過通知。")
            return
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
                json={"chat_id": self.settings.telegram_chat_id, "text": text}, timeout=15,
            )
            response.raise_for_status()
            self.log("Telegram 通知已發送。")
        except requests.RequestException as exc:
            self.log(f"Telegram 通知失敗：{exc}")

    def load_seen(self) -> dict[str, None]:
        try:
            # A dict preserves JSON order while retaining fast membership
            # checks. The newest IDs are appended at the end.
            return dict.fromkeys(str(work_id) for work_id in json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_seen(self, seen: dict[str, None]) -> None:
        # Keep the most recently added IDs. Unlike a set, this never drops a
        # newly processed work merely because the collection reached its cap.
        STATE_PATH.write_text(json.dumps(list(seen)[-MAX_SEEN_IDS:]), encoding="utf-8")

    def init_driver(self) -> None:
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()
        options.add_argument("--disable-gpu")
        chrome_major = self.detect_chrome_major_version()
        if chrome_major is None:
            self.log("無法偵測 Chrome 版本，將由驅動程式自動選擇版本。")
            self.driver = uc.Chrome(options=options)
        else:
            self.log(f"偵測到 Chrome {chrome_major}，正在下載／使用相符的 ChromeDriver…")
            self.driver = uc.Chrome(version_main=chrome_major, options=options)
        self.driver.set_page_load_timeout(30)
        self.driver.get("https://www.pixiv.net/")
        if self.stop_event.wait(2):
            return
        self.driver.add_cookie({"name": "PHPSESSID", "value": self.settings.php_sessid, "domain": ".pixiv.net"})
        self.log("瀏覽器已初始化。")

    @staticmethod
    def detect_chrome_major_version() -> int | None:
        """Read Chrome's installed version from the Windows registry."""
        registry_locations = (
            (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon"),
        )
        for hive, location in registry_locations:
            try:
                with winreg.OpenKey(hive, location) as key:
                    version, _ = winreg.QueryValueEx(key, "version")
                return int(str(version).split(".", maxsplit=1)[0])
            except (FileNotFoundError, OSError, ValueError):
                continue
        return None

    def click_stamp(self, work_id: str) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            if self.driver is None:
                self.init_driver()
            if self.stop_event.is_set():
                return False
            self.driver.get(f"https://www.pixiv.net/artworks/{work_id}")
            if self.stop_event.wait(3):
                return False
            wait = WebDriverWait(self.driver, 1)
            steps = [
                [
                    (By.XPATH, "/html/body/div[1]/div/div[2]/div[5]/div[1]/div/div[1]/main/section/div[2]/div[2]/section/div/div/div[1]/form/div/nav/div"),
                ],
                [
                    (By.XPATH, "/html/body/div[6]/div/div/div/nav/button[2]"),
                    (By.XPATH, "/html/body/div[5]/div/div/div/nav/button[2]"),
                ],
                [
                    (By.XPATH, "/html/body/div[6]/div/div/div/div/div/div/div[3]/button"),
                    (By.XPATH, "/html/body/div[5]/div/div/div/div/div/div/div[3]/button"),
                ],
            ]
            for index, candidates in enumerate(steps, start=1):
                if self.stop_event.is_set():
                    return False
                last_error = None
                clicked = False
                for by, selector in candidates:
                    try:
                        element = wait.until(EC.element_to_be_clickable((by, selector)))
                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                        element.click()
                        clicked = True
                        break
                    except Exception as exc:
                        last_error = exc
                if not clicked:
                    raise last_error or RuntimeError(f"找不到貼圖步驟 {index} 的按鈕")
                self.log(f"已完成貼圖步驟 {index}/3。")
                if self.stop_event.wait(1):
                    return False
            return True
        except Exception as exc:
            self.log(f"瀏覽器操作失敗：{exc}")
            return False

    def close_driver(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
                self.log("瀏覽器已關閉。")
            except Exception as exc:
                self.log(f"關閉瀏覽器失敗：{exc}")
            finally:
                self.driver = None


class Application(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.window_icon_image = ImageTk.PhotoImage(Image.open(APP_ICON_PATH).convert("RGBA"))
        self.iconphoto(True, self.window_icon_image)
        self.geometry("760x650")
        self.minsize(640, 480)
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stop_event: threading.Event | None = None
        self.closing = False
        self.tray_icon = self.create_tray_icon()
        self.settings = Settings.load()
        self.build_ui()
        self.tray_icon.run_detached()
        self.after(150, self.flush_logs)
        if self.settings.auto_start_on_login:
            self.after(500, self.start)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def create_tray_icon(self) -> pystray.Icon:
        """Create the notification-area menu used while the window is hidden."""
        image = Image.open(APP_ICON_PATH).convert("RGBA")
        menu = pystray.Menu(
            pystray.MenuItem("顯示視窗", self.show_window, default=True),
            pystray.MenuItem("結束程式", self.exit_application),
        )
        return pystray.Icon("pixiv_autostamp", image, APP_NAME, menu)

    def hide_to_tray(self) -> None:
        self.withdraw()
        self.enqueue_log("視窗已隱藏至系統匣；服務會繼續在背景執行。")

    def show_window(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        self.after(0, self._show_window)

    def _show_window(self) -> None:
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def exit_application(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        self.after(0, self.shutdown)

    def build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        settings_frame = ttk.LabelFrame(root, text="設定（儲存於 exe 同資料夾的 config.json）", padding=10)
        settings_frame.pack(fill="x")
        self.entries: dict[str, ttk.Entry] = {}
        self.auto_start_on_login = tk.BooleanVar(value=self.settings.auto_start_on_login)
        ttk.Checkbutton(
            settings_frame,
            text="開機後自動啟動監控",
            variable=self.auto_start_on_login,
        ).grid(row=0, column=1, sticky="w", pady=(0, 5))
        fields = [("php_sessid", "Pixiv PHPSESSID", True), ("telegram_bot_token", "Telegram Bot Token", True), ("telegram_chat_id", "Telegram Chat ID", False)]
        row = 1
        for key, label, masked in fields:
            ttk.Label(settings_frame, text=label, width=20).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            entry = ttk.Entry(settings_frame, show="•" if masked else "")
            entry.insert(0, getattr(self.settings, key))
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            self.entries[key] = entry
            row += 1
            if key == "php_sessid":
                ttk.Label(settings_frame, text="備註", width=20).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=3)
                self.notes_text = tk.Text(settings_frame, height=2, wrap="word", font=("Microsoft JhengHei UI", 10))
                self.notes_text.insert("1.0", self.settings.notes)
                self.notes_text.grid(row=row, column=1, sticky="ew", pady=3)
                row += 1
        ttk.Label(settings_frame, text="關鍵字", width=20).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=3)
        self.keywords_text = tk.Text(settings_frame, height=3, wrap="word", font=("Microsoft JhengHei UI", 10))
        self.keywords_text.insert("1.0", self.settings.keyword)
        self.keywords_text.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1
        ttk.Label(settings_frame, text="每行填寫一個關鍵字；程式會依序搜尋。 ").grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Label(settings_frame, text="排除作者", width=20).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=3)
        self.excluded_authors_text = tk.Text(settings_frame, height=3, wrap="word", font=("Microsoft JhengHei UI", 10))
        self.excluded_authors_text.insert("1.0", self.settings.excluded_authors)
        self.excluded_authors_text.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1
        ttk.Label(settings_frame, text="每行填寫一位作者；符合者只通知，不會貼圖。").grid(row=row, column=1, sticky="w")
        settings_frame.columnconfigure(1, weight=1)
        controls = ttk.Frame(root)
        controls.pack(fill="x", pady=12)
        self.start_button = ttk.Button(controls, text="啟動", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.save_button = ttk.Button(controls, text="儲存設定", command=self.save_settings)
        self.save_button.pack(side="left")
        self.status = tk.StringVar(value="狀態：未啟動")
        ttk.Label(controls, textvariable=self.status).pack(side="right")
        ttk.Label(root, text="執行日誌").pack(anchor="w")
        self.log_text = tk.Text(root, height=18, state="disabled", wrap="word", font=("Microsoft JhengHei UI", 10))
        self.log_text.pack(fill="both", expand=True, pady=(4, 0))

    def start(self) -> None:
        settings = self.collect_settings()
        if not settings.keyword.strip() or not settings.php_sessid:
            messagebox.showwarning(APP_NAME, "請先填寫關鍵字與 Pixiv PHPSESSID。")
            return
        if not self.persist_settings(settings):
            return
        self.stop_event = threading.Event()
        worker = PixivWorker(self.settings, self.enqueue_log, self.stop_event)
        self.worker_thread = threading.Thread(target=worker.run, daemon=True)
        self.worker_thread.start()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set("狀態：執行中")
        self.enqueue_log("設定已儲存。")

    def save_settings(self) -> None:
        self.persist_settings(self.collect_settings())

    def collect_settings(self) -> Settings:
        values = {key: entry.get().strip() for key, entry in self.entries.items()}
        values["notes"] = self.notes_text.get("1.0", "end-1c")
        values["keyword"] = self.keywords_text.get("1.0", "end-1c")
        values["excluded_authors"] = self.excluded_authors_text.get("1.0", "end-1c")
        values["auto_start_on_login"] = self.auto_start_on_login.get()
        return Settings(**values)

    def persist_settings(self, settings: Settings) -> bool:
        try:
            configure_autostart(settings.auto_start_on_login)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"無法更新開機自動執行設定：{exc}")
            return False
        self.settings = settings
        self.settings.save()
        self.enqueue_log("設定已儲存。")
        return True

    def stop(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
            self.status.set("狀態：正在停止…")
            self.stop_button.configure(state="disabled")
            self.enqueue_log("已要求停止，請等待目前操作結束。")
            self.after(200, self.check_stopped)

    def check_stopped(self) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self.after(200, self.check_stopped)
        else:
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.status.set("狀態：未啟動")

    def enqueue_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}\n")

    def flush_logs(self) -> None:
        while not self.log_queue.empty():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", self.log_queue.get_nowait())
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(150, self.flush_logs)

    def on_close(self) -> None:
        self.hide_to_tray()

    def shutdown(self) -> None:
        """Stop the service and close the app after the tray Exit command."""
        if self.closing:
            return
        self.closing = True
        if self.worker_thread is not None and self.worker_thread.is_alive():
            if self.stop_event is not None:
                self.stop_event.set()
            self.after(250, self.finish_shutdown)
            return
        self.finish_shutdown()

    def finish_shutdown(self) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self.after(250, self.finish_shutdown)
            return
        self.tray_icon.stop()
        self.destroy()


if __name__ == "__main__":
    if acquire_single_instance():
        Application().mainloop()
