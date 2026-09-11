"""Pixiv auto-stamp desktop application.

The configuration file is intentionally kept beside the executable so that
credentials are not embedded in the distributable program.
"""

from __future__ import annotations

import ctypes
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import winreg
from dataclasses import dataclass, asdict
from datetime import datetime
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
MAX_SEEN_IDS = 1000
SEARCH_RETRY_DELAYS_SECONDS = (2, 5)
ACTIVITY_REQUEST_DELAY_SECONDS = 0.6
# Keep this conservative: the comments API can reject overly large pages.
ACTIVITY_COMMENT_PAGE_SIZE = 30
STAMP_PRIMARY_WAIT_SECONDS = (5, 1, 1)
STAMP_STEP_DELAY_SECONDS = (0.35, 0.35, 0)
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
PIXIV_HOME_URL = "https://www.pixiv.net/"


def search_api_url(keyword: str) -> str:
    """Build the exact Pixiv search API URL used by the worker."""
    encoded_keyword = quote(keyword)
    return (
        f"https://www.pixiv.net/ajax/search/artworks/{encoded_keyword}"
        f"?word={encoded_keyword}&mode=tag&order=date_d&p=1"
    )


def format_upload_datetime(value: object) -> str:
    """Format Pixiv's ISO upload timestamp in the computer's local time."""
    text = str(value or "")
    if not text:
        return "上傳時間未知"
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text.replace("T", " ")


def create_pixiv_session(php_sessid: str) -> requests.Session:
    """Create an authenticated session for Pixiv's JSON API."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.pixiv.net/",
        "Accept": "application/json",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    })
    session.cookies.update({"PHPSESSID": php_sessid})
    return session


def load_seen_ids() -> dict[str, None]:
    try:
        # A dict preserves JSON order while retaining fast membership checks.
        return dict.fromkeys(str(work_id) for work_id in json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def save_seen_ids(seen: dict[str, None]) -> None:
    """Keep only the most recently added IDs in processing order."""
    STATE_PATH.write_text(json.dumps(list(seen)[-MAX_SEEN_IDS:]), encoding="utf-8")


def firefox_executable() -> Path | None:
    """Find Firefox in PATH or either standard Windows installation folder."""
    found = shutil.which("firefox.exe")
    candidates = [
        Path(found) if found else None,
        Path(r"C:\Program Files\Mozilla Firefox\firefox.exe"),
        Path(r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"),
    ]
    return next((path for path in candidates if path is not None and path.is_file()), None)


class Tooltip:
    """A small hover hint for Tk widgets."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def show(self, _event: tk.Event | None = None) -> None:
        if self.window is not None:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(
            f"+{self.widget.winfo_rootx() + 20}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}"
        )
        tk.Label(
            self.window,
            text=self.text,
            padx=8,
            pady=5,
            relief="solid",
            borderwidth=1,
            background="#ffffe0",
        ).pack()

    def hide(self, _event: tk.Event | None = None) -> None:
        if self.window is not None:
            self.window.destroy()
            self.window = None


@dataclass
class Settings:
    keyword: str = "御稜ナグサ"
    php_sessid: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notes: str = ""
    excluded_user_ids: str = ""
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

    def excluded_user_id_set(self) -> set[str]:
        """Return non-empty Pixiv user IDs entered one per line."""
        return set(self.excluded_user_id_list())

    def excluded_user_id_list(self) -> list[str]:
        """Return user IDs in input order, with duplicates removed."""
        return list(dict.fromkeys(
            user_id.strip() for user_id in self.excluded_user_ids.splitlines() if user_id.strip()
        ))

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
        self.session = create_pixiv_session(settings.php_sessid)

    def run(self) -> None:
        self.log("服務已啟動，將每 30 秒檢查一次新作品。")
        try:
            while not self.stop_event.is_set():
                try:
                    self.check_once()
                except requests.HTTPError as exc:
                    status_code = exc.response.status_code if exc.response is not None else None
                    if status_code == 401:
                        self.log(
                            "Pixiv 驗證失敗（401）：PHPSESSID 已失效或不正確。"
                            "請重新從已登入 Pixiv 的瀏覽器取得 PHPSESSID，貼到設定欄位後按「儲存設定」，再重新啟動服務。"
                        )
                        break
                    self.log(f"Pixiv 網路請求失敗（HTTP {status_code or '未知'}），將於 30 秒後重試。")
                except requests.RequestException as exc:
                    self.log(f"Pixiv 連線暫時中斷：{exc}，將於 30 秒後重試。")
                if self.stop_event.wait(POLL_INTERVAL_SECONDS):
                    break
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
        excluded_user_ids = self.settings.excluded_user_id_set()
        for keyword in self.settings.keywords():
            if self.stop_event.is_set():
                return
            url = search_api_url(keyword)
            self.log(f"正在查詢關鍵字「{keyword}」…")
            response = self.get_search_response(url, keyword)
            if response is None:
                self.save_seen(seen)
                self.close_driver()
                return
            works = response.json()["body"]["illustManga"]["data"]
            self.log(f"「{keyword}」找到 {len(works)} 個作品。")
            new_works = [work for work in works if str(work["id"]) not in seen]
            if not new_works:
                continue

            found_new_work = True
            self.log(f"「{keyword}」偵測到 {len(new_works)} 個新作品。")
            for work in reversed(new_works):
                if self.stop_event.is_set():
                    return
                work_id = str(work["id"])
                title, author = work["title"], work["userName"]
                author_user_id = str(work["userId"])
                work_url = f"https://www.pixiv.net/artworks/{work_id}"
                if author_user_id in excluded_user_ids:
                    self.notify_new_work(keyword, title, author, work_url)
                    self.log(f"略過排除 userId 的作品：{title}（{author}，userId: {author_user_id}）")
                    seen[work_id] = None
                    continue
                comment_info = self.get_comment_info(work_id)
                if comment_info is None:
                    self.log(f"無法確認評論數，保留作品供下次重試：{title}")
                    continue
                comment_count, comments_disabled = comment_info
                if comments_disabled:
                    self.notify_comments_disabled(keyword, title, author, work_url)
                    self.log(f"略過已關閉留言的作品：{title}")
                    seen[work_id] = None
                    continue
                if comment_count > 0:
                    self.notify(
                        f"【Pixiv 新作品（略過貼圖）】\n\n關鍵字: {keyword}\n標題: {title}\n"
                        f"作者: {author}\n評論數: {comment_count}（大於 0）\n\n{work_url}"
                    )
                    self.log(f"略過已有 {comment_count} 則評論的作品：{title}")
                    seen[work_id] = None
                    continue
                self.notify_new_work(keyword, title, author, work_url)
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

    def get_search_response(self, url: str, keyword: str) -> requests.Response | None:
        """Fetch a search page with brief retries for temporary network resets."""
        attempt_count = len(SEARCH_RETRY_DELAYS_SECONDS) + 1
        for attempt in range(attempt_count):
            try:
                response = self.session.get(url, timeout=15)
                response.raise_for_status()
                return response
            except requests.HTTPError:
                raise
            except requests.RequestException as exc:
                if attempt == attempt_count - 1:
                    self.log(
                        f"「{keyword}」連線失敗：{exc}；本輪略過，將於 30 秒後重試。"
                    )
                    return None
                delay = SEARCH_RETRY_DELAYS_SECONDS[attempt]
                self.log(
                    f"「{keyword}」連線暫時中斷（第 {attempt + 1}/{attempt_count} 次）：{exc}；"
                    f"{delay} 秒後重試。"
                )
                if self.stop_event.wait(delay):
                    return None
        return None

    def get_comment_info(self, work_id: str) -> tuple[int, bool] | None:
        """Return (comment count, comments disabled) from full artwork details."""
        try:
            response = self.session.get(
                f"https://www.pixiv.net/ajax/illust/{work_id}?full=1", timeout=15
            )
            response.raise_for_status()
            body = response.json().get("body", {})
            if not isinstance(body, dict) or "commentCount" not in body:
                raise ValueError("Pixiv 回應未包含 commentCount")
            return int(body["commentCount"]), bool(body.get("commentOff"))
        except (requests.RequestException, TypeError, ValueError) as exc:
            self.log(f"無法確認評論數，將於下次輪詢重試：{exc}")
            return None

    def notify_new_work(self, keyword: str, title: str, author: str, work_url: str) -> None:
        """Send the standard new-work notification without comment details."""
        self.notify(
            f"【Pixiv 新作品】\n\n關鍵字: {keyword}\n標題: {title}\n作者: {author}\n\n{work_url}"
        )

    def notify_comments_disabled(
        self, keyword: str, title: str, author: str, work_url: str
    ) -> None:
        """Notify that stamping is unavailable because the author disabled comments."""
        self.notify(
            f"【Pixiv 新作品（無法貼圖）】\n\n關鍵字: {keyword}\n標題: {title}\n作者: {author}\n"
            f"作者已關閉評論，無法貼圖。\n\n{work_url}"
        )

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
        return load_seen_ids()

    def save_seen(self, seen: dict[str, None]) -> None:
        save_seen_ids(seen)

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
                for candidate_index, (by, selector) in enumerate(candidates):
                    try:
                        # Step 1 waits for the page to hydrate; fallback XPath
                        # attempts (steps 2 and 3) intentionally remain 1 second apart.
                        wait_seconds = (
                            STAMP_PRIMARY_WAIT_SECONDS[index - 1]
                            if candidate_index == 0
                            else 1
                        )
                        wait = WebDriverWait(self.driver, wait_seconds, poll_frequency=0.1)
                        element = wait.until(EC.element_to_be_clickable((by, selector)))
                        element.click()
                        clicked = True
                        break
                    except Exception as exc:
                        last_error = exc
                if not clicked:
                    raise last_error or RuntimeError(f"找不到貼圖步驟 {index} 的按鈕")
                self.log(f"已完成貼圖步驟 {index}/3。")
                if self.stop_event.wait(STAMP_STEP_DELAY_SECONDS[index - 1]):
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
        self.api_add_in_progress = False
        self.activity_check_in_progress = False
        self.activity_dialog: tk.Toplevel | None = None
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
                notes_label = ttk.Frame(settings_frame)
                notes_label.grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=3)
                ttk.Label(notes_label, text="備註", width=8).pack(side="left")
                ttk.Button(
                    notes_label,
                    text="Firefox 無痕開啟 Pixiv",
                    command=self.open_pixiv_in_private_firefox,
                ).pack(side="left")
                self.notes_text = tk.Text(settings_frame, height=2, wrap="word", font=("Microsoft JhengHei UI", 10))
                self.notes_text.insert("1.0", self.settings.notes)
                self.notes_text.grid(row=row, column=1, sticky="ew", pady=3)
                row += 1
        keyword_label = ttk.Frame(settings_frame)
        keyword_label.grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=3)
        ttk.Label(keyword_label, text="關鍵字", width=20).pack(anchor="w")
        ttk.Button(keyword_label, text="查看 API 結果", command=self.open_keyword_api_results).pack(anchor="w")
        self.add_api_results_button = ttk.Button(
            keyword_label, text="添加API結果", command=self.add_api_results
        )
        self.add_api_results_button.pack(anchor="w", pady=(3, 0))
        Tooltip(
            self.add_api_results_button,
            "停止狀態可用。會清除所有舊 ID，再加入目前關鍵字第一頁作品，避免過多新作品。",
        )
        self.keywords_text = tk.Text(settings_frame, height=3, wrap="word", font=("Microsoft JhengHei UI", 10))
        self.keywords_text.insert("1.0", self.settings.keyword)
        self.keywords_text.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1
        ttk.Label(settings_frame, text="每行填寫一個關鍵字；程式會依序搜尋。 ").grid(row=row, column=1, sticky="w")
        row += 1
        excluded_user_id_label = ttk.Frame(settings_frame)
        excluded_user_id_label.grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=3)
        ttk.Label(excluded_user_id_label, text="排除 userId", width=20).pack(anchor="w")
        self.activity_check_button = ttk.Button(
            excluded_user_id_label, text="檢查活動", command=self.open_activity_check
        )
        self.activity_check_button.pack(anchor="w", pady=(3, 0))
        self.excluded_user_ids_text = tk.Text(settings_frame, height=3, wrap="word", font=("Microsoft JhengHei UI", 10))
        self.excluded_user_ids_text.insert("1.0", self.settings.excluded_user_ids)
        self.excluded_user_ids_text.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1
        ttk.Label(settings_frame, text="每行填寫一個 Pixiv userId；符合者只通知，不會貼圖。").grid(row=row, column=1, sticky="w")
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
        if self.api_add_in_progress:
            messagebox.showinfo(APP_NAME, "正在加入 API 結果，完成後才能啟動服務。")
            return
        if getattr(self, "activity_check_in_progress", False):
            messagebox.showinfo(APP_NAME, "正在檢查活動，完成後才能啟動服務。")
            return
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
        self.add_api_results_button.configure(state="disabled")
        self.activity_check_button.configure(state="disabled")
        self.status.set("狀態：執行中")
        self.enqueue_log("設定已儲存。")

    def save_settings(self) -> None:
        self.persist_settings(self.collect_settings())

    def open_keyword_api_results(self) -> None:
        keywords = Settings(keyword=self.keywords_text.get("1.0", "end-1c")).keywords()
        if not keywords:
            messagebox.showwarning(APP_NAME, "請先填寫至少一個關鍵字。")
            return
        for keyword in keywords:
            webbrowser.open_new_tab(search_api_url(keyword))
        self.enqueue_log(f"已開啟 {len(keywords)} 個關鍵字的 Pixiv API 搜尋結果。")

    def open_activity_check(self) -> None:
        """Open the low-frequency activity lookup for one user and keyword."""
        if self.worker_thread is not None and self.worker_thread.is_alive():
            messagebox.showwarning(APP_NAME, "服務執行中，請先停止服務再檢查活動。")
            return
        if self.api_add_in_progress:
            messagebox.showinfo(APP_NAME, "正在加入 API 結果，完成後才能檢查活動。")
            return
        if self.activity_dialog is not None and self.activity_dialog.winfo_exists():
            self.activity_dialog.deiconify()
            self.activity_dialog.lift()
            self.activity_dialog.focus_force()
            return

        user_ids = Settings(
            excluded_user_ids=self.excluded_user_ids_text.get("1.0", "end-1c")
        ).excluded_user_id_list()
        keywords = Settings(keyword=self.keywords_text.get("1.0", "end-1c")).keywords()
        if not user_ids:
            messagebox.showwarning(APP_NAME, "請先在「排除 userId」填寫至少一個 userId。")
            return
        if not keywords:
            messagebox.showwarning(APP_NAME, "請先填寫至少一個關鍵字。")
            return

        dialog = tk.Toplevel(self)
        self.activity_dialog = dialog
        dialog.title("檢查活動")
        dialog.geometry("760x560")
        dialog.minsize(640, 420)
        dialog.transient(self)
        dialog.protocol("WM_DELETE_WINDOW", self.close_activity_dialog)

        root = ttk.Frame(dialog, padding=16)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="活動檢查", font=("Microsoft JhengHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            root,
            text="掃描所選關鍵字第一頁作品的所有留言與貼圖（包括回覆）；為避免影響 30 秒排程，僅能在服務停止時查詢。",
            wraplength=700,
        ).pack(anchor="w", pady=(3, 12))

        query_frame = ttk.LabelFrame(root, text="查詢條件", padding=10)
        query_frame.pack(fill="x")
        ttk.Label(query_frame, text="查詢 userId", width=14).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        dialog.activity_user_id = tk.StringVar(value=user_ids[0])
        user_id_box = ttk.Combobox(
            query_frame,
            textvariable=dialog.activity_user_id,
            values=user_ids,
            state="readonly",
        )
        user_id_box.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(query_frame, text="查詢關鍵字", width=14).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        dialog.activity_keyword = tk.StringVar(value=keywords[0])
        keyword_box = ttk.Combobox(
            query_frame,
            textvariable=dialog.activity_keyword,
            values=keywords,
            state="normal",
        )
        keyword_box.grid(row=1, column=1, sticky="ew", pady=4)
        query_frame.columnconfigure(1, weight=1)

        action_frame = ttk.Frame(root)
        action_frame.pack(fill="x", pady=(10, 6))
        dialog.activity_query_button = ttk.Button(
            action_frame,
            text="確定查詢",
            command=lambda: self.start_activity_check(dialog),
        )
        dialog.activity_query_button.pack(side="left")
        dialog.activity_status = tk.StringVar(value="選擇 userId 與關鍵字後開始查詢。")
        ttk.Label(action_frame, textvariable=dialog.activity_status).pack(side="left", padx=10)

        ttk.Label(root, text="找到活動的作品", font=("Microsoft JhengHei UI", 10, "bold")).pack(anchor="w", pady=(4, 2))
        dialog.activity_results = tk.Text(
            root, height=16, state="disabled", wrap="word", font=("Microsoft JhengHei UI", 10)
        )
        dialog.activity_results.pack(fill="both", expand=True)

    def close_activity_dialog(self) -> None:
        if self.activity_dialog is not None and self.activity_dialog.winfo_exists():
            self.activity_dialog.destroy()
        self.activity_dialog = None

    def start_activity_check(self, dialog: tk.Toplevel) -> None:
        if self.activity_check_in_progress:
            return
        user_id = dialog.activity_user_id.get().strip()
        keyword = dialog.activity_keyword.get().strip()
        settings = self.collect_settings()
        if not user_id or not keyword:
            messagebox.showwarning(APP_NAME, "請選擇 userId 並填寫關鍵字。", parent=dialog)
            return
        if not settings.php_sessid:
            messagebox.showwarning(APP_NAME, "請先填寫 Pixiv PHPSESSID。", parent=dialog)
            return
        self.activity_check_in_progress = True
        dialog.activity_query_button.configure(state="disabled")
        dialog.activity_status.set("正在讀取關鍵字第一頁作品…")
        self.start_button.configure(state="disabled")
        self.add_api_results_button.configure(state="disabled")
        self.activity_check_button.configure(state="disabled")
        self.enqueue_log(f"正在檢查 userId {user_id} 在「{keyword}」第一頁作品的活動…")
        threading.Thread(
            target=self.activity_check_worker,
            args=(dialog, settings.php_sessid, user_id, keyword),
            daemon=True,
        ).start()

    def activity_check_worker(
        self, dialog: tk.Toplevel, php_sessid: str, user_id: str, keyword: str
    ) -> None:
        results: list[tuple[str, str]] = []
        work_count = 0
        skipped_work_count = 0
        error_message: str | None = None
        try:
            session = create_pixiv_session(php_sessid)
            response = session.get(search_api_url(keyword), timeout=15)
            response.raise_for_status()
            works = response.json()["body"]["illustManga"]["data"]
            unique_works = {str(work["id"]): work for work in works}
            candidate_works = [
                work for work in unique_works.values()
                if str(work.get("userId", "")) != user_id
            ]
            candidate_works.sort(key=lambda work: str(work.get("createDate", "")), reverse=True)
            work_count = len(candidate_works)
            for index, work in enumerate(candidate_works, start=1):
                work_id = str(work["id"])
                self.after(
                    0,
                    lambda current=index, total=work_count: self.update_activity_status(
                        dialog, f"正在掃描作品 {current}/{total}…"
                    ),
                )
                try:
                    has_activity = self.find_user_root_activity(session, work_id, user_id)
                except requests.HTTPError as exc:
                    status_code = exc.response.status_code if exc.response is not None else None
                    if status_code == 401:
                        raise
                    skipped_work_count += 1
                    self.enqueue_log(
                        f"活動檢查略過作品 {work_id}：Pixiv API 回應 HTTP {status_code or '未知'}。"
                    )
                    has_activity = False
                if has_activity:
                    results.append((str(work.get("createDate", "")), work_id))
                if index < work_count:
                    time.sleep(ACTIVITY_REQUEST_DELAY_SECONDS)
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 401:
                error_message = "PHPSESSID 已失效或不正確，請重新取得並儲存。"
            else:
                error_message = f"Pixiv API 回應 HTTP {status_code or '未知'}。"
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            error_message = f"查詢失敗：{exc}"
        finally:
            self.after(
                0,
                self.finish_activity_check,
                dialog,
                results,
                work_count,
                skipped_work_count,
                error_message,
            )

    def find_user_root_activity(
        self, session: requests.Session, work_id: str, user_id: str
    ) -> bool:
        """Return whether the target user has a comment or stamp on one work."""
        offset = 0
        while True:
            response = session.get(
                "https://www.pixiv.net/ajax/illusts/comments/roots",
                params={
                    "illust_id": work_id,
                    "offset": offset,
                    "limit": ACTIVITY_COMMENT_PAGE_SIZE,
                    "lang": "zh",
                },
                headers={"Referer": f"https://www.pixiv.net/artworks/{work_id}"},
                timeout=15,
            )
            response.raise_for_status()
            body = response.json().get("body", {})
            comments = body.get("comments", [])
            for comment in comments:
                if str(comment.get("userId", "")) == user_id:
                    return True
                if comment.get("hasReplies") and self.find_user_reply_activity(
                    session, work_id, str(comment.get("id", "")), user_id
                ):
                    return True
            if not body.get("hasNext") or not comments:
                return False
            offset += len(comments)
            time.sleep(ACTIVITY_REQUEST_DELAY_SECONDS)

    def find_user_reply_activity(
        self, session: requests.Session, work_id: str, parent_comment_id: str, user_id: str
    ) -> bool:
        """Scan every reply page below one root comment for the selected user."""
        if not parent_comment_id:
            return False
        page = 1
        while True:
            time.sleep(ACTIVITY_REQUEST_DELAY_SECONDS)
            response = session.get(
                "https://www.pixiv.net/ajax/illusts/comments/replies",
                params={"comment_id": parent_comment_id, "page": page, "lang": "zh"},
                headers={"Referer": f"https://www.pixiv.net/artworks/{work_id}"},
                timeout=15,
            )
            response.raise_for_status()
            body = response.json().get("body", {})
            comments = body.get("comments", [])
            if any(str(comment.get("userId", "")) == user_id for comment in comments):
                return True
            if not body.get("hasNext") or not comments:
                return False
            page += 1

    def update_activity_status(self, dialog: tk.Toplevel, message: str) -> None:
        if self.activity_dialog is dialog and dialog.winfo_exists():
            dialog.activity_status.set(message)

    def finish_activity_check(
        self,
        dialog: tk.Toplevel,
        results: list[tuple[str, str]],
        work_count: int,
        skipped_work_count: int,
        error_message: str | None,
    ) -> None:
        self.activity_check_in_progress = False
        self.start_button.configure(state="normal")
        self.add_api_results_button.configure(state="normal")
        self.activity_check_button.configure(state="normal")
        if self.activity_dialog is not dialog or not dialog.winfo_exists():
            return
        dialog.activity_query_button.configure(state="normal")
        if error_message is not None:
            dialog.activity_status.set(error_message)
            self.enqueue_log(f"活動檢查失敗：{error_message}")
            return
        status_message = f"已掃描 {work_count} 件作品，找到 {len(results)} 件有活動。"
        if skipped_work_count:
            status_message += f" 已略過 {skipped_work_count} 件無法讀取的作品。"
        dialog.activity_status.set(status_message)
        results_text = dialog.activity_results
        results_text.configure(state="normal")
        results_text.delete("1.0", "end")
        if not results:
            results_text.insert("end", "未找到該 userId 的留言或貼圖活動。")
        for index, (create_date, work_id) in enumerate(results):
            work_url = f"https://www.pixiv.net/artworks/{work_id}"
            results_text.insert("end", f"{format_upload_datetime(create_date)}  ")
            start = results_text.index("end-1c")
            results_text.insert("end", f"{work_url}\n")
            end = results_text.index("end-1c")
            tag_name = f"activity_link_{index}"
            results_text.tag_add(tag_name, start, end)
            results_text.tag_config(tag_name, foreground="#1769aa", underline=True)
            results_text.tag_bind(
                tag_name,
                "<Button-1>",
                lambda _event, url=work_url: webbrowser.open_new_tab(url),
            )
        results_text.configure(state="disabled")
        self.enqueue_log(f"活動檢查完成：掃描 {work_count} 件作品，找到 {len(results)} 件有活動。")

    def open_pixiv_in_private_firefox(self) -> None:
        executable = firefox_executable()
        if executable is None:
            messagebox.showerror(APP_NAME, "找不到 Firefox，請安裝 Firefox 後再試一次。")
            return
        try:
            subprocess.Popen([str(executable), "-private-window", PIXIV_HOME_URL])
            self.enqueue_log("已以 Firefox 無痕視窗開啟 Pixiv。")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"無法開啟 Firefox：{exc}")

    def add_api_results(self) -> None:
        """Record every work returned from page one without stamping it."""
        if self.worker_thread is not None and self.worker_thread.is_alive():
            messagebox.showwarning(APP_NAME, "服務執行中，請先停止服務再添加 API 結果。")
            return
        settings = self.collect_settings()
        if not settings.keyword.strip() or not settings.php_sessid:
            messagebox.showwarning(APP_NAME, "請先填寫關鍵字與 Pixiv PHPSESSID。")
            return
        self.api_add_in_progress = True
        self.start_button.configure(state="disabled")
        self.add_api_results_button.configure(state="disabled")
        self.activity_check_button.configure(state="disabled")
        self.status.set("狀態：正在添加 API 結果…")
        self.enqueue_log("正在讀取 API 第一頁全部作品；成功後將清除舊 ID 並寫入目前結果…")
        threading.Thread(
            target=self.add_api_results_worker, args=(settings,), daemon=True
        ).start()

    def add_api_results_worker(self, settings: Settings) -> None:
        try:
            session = create_pixiv_session(settings.php_sessid)
            # Do not overwrite the existing file until every API request has
            # succeeded; a temporary network error must not erase prior IDs.
            seen: dict[str, None] = {}
            result_count = 0
            for keyword in settings.keywords():
                response = session.get(search_api_url(keyword), timeout=15)
                response.raise_for_status()
                works = response.json()["body"]["illustManga"]["data"]
                result_count += len(works)
                for work in works:
                    work_id = str(work["id"])
                    seen[work_id] = None
            save_seen_ids(seen)
            self.enqueue_log(
                f"已清除舊 ID，並將 {result_count} 筆 API 結果中的 {len(seen)} 筆作品寫入 seen_id.json（最多保留 {MAX_SEEN_IDS} 筆）。"
            )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 401:
                self.enqueue_log(
                    "添加 API 結果失敗：PHPSESSID 已失效或不正確。請重新取得並儲存 PHPSESSID。"
                )
            else:
                self.enqueue_log(f"添加 API 結果失敗（HTTP {status_code or '未知'}）：{exc}")
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            self.enqueue_log(f"添加 API 結果失敗：{exc}")
        finally:
            self.after(0, self.finish_adding_api_results)

    def finish_adding_api_results(self) -> None:
        self.api_add_in_progress = False
        self.start_button.configure(state="normal")
        self.add_api_results_button.configure(state="normal")
        self.activity_check_button.configure(state="normal")
        self.status.set("狀態：未啟動")

    def collect_settings(self) -> Settings:
        values = {key: entry.get().strip() for key, entry in self.entries.items()}
        values["notes"] = self.notes_text.get("1.0", "end-1c")
        values["keyword"] = self.keywords_text.get("1.0", "end-1c")
        values["excluded_user_ids"] = self.excluded_user_ids_text.get("1.0", "end-1c")
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
            self.add_api_results_button.configure(state="normal")
            self.activity_check_button.configure(state="normal")
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
