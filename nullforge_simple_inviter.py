"""
Nullforge Simple Inviter

Single-file, single-account VRChat group auto-inviter. No server, no
license system — just log in, pick a group, watch your local VRChat log,
invite people who join, respecting VRChat's own rate limits by pausing and
waiting rather than working around them.

UI is a local web view (HTML/CSS/JS in ui/) for real glassmorphism/background
support that plain Tkinter can't do — backend logic below is unchanged in
substance from the original Tkinter version, just exposed to the UI as a
JS-callable API instead of wired to widgets directly.

Package with:
    pyinstaller --onefile --windowed --name "NullforgeSimpleInviter" \\
        --icon=assets/nullforge_icon.ico \\
        --add-data "ui;ui" \\
        nullforge_simple_inviter.py

NOTE: VRChat's login/2FA flow and the log-line join pattern are both based
on commonly-referenced (not officially documented) formats — validate
against a real run before relying on this.
"""

import base64
import glob
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import webview
from cryptography.fernet import Fernet

# ─────────────────────────────────────────────
# Resource paths — works both as a plain script and inside a PyInstaller
# --onefile bundle (which extracts data files to sys._MEIPASS at runtime)
# ─────────────────────────────────────────────
def _resource_path(*parts) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


UI_INDEX = _resource_path("ui", "index.html")
ICON_PATH = _resource_path("assets", "nullforge_icon.ico")

# ─────────────────────────────────────────────
# Local storage
# ─────────────────────────────────────────────
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "NullforgeSimpleInviter")
STATE_FILE = os.path.join(APP_DIR, "state.enc.json")
LOCAL_KEY_FILE = os.path.join(APP_DIR, "local.key")

DEFAULT_STATE = {
    "vrchat_log_folder": "",
    "group_id": "",
    "group_name": "",
    "auth_cookie": "",
    "auth_display_name": "",
    "auto_invite_enabled": False,
    "invite_delay_seconds": 40,
    "invited_user_ids": [],
    "daily_invite_count": 0,
    "daily_count_reset_at": None,
    "rate_limited_until": None,
}


def _get_fernet() -> Fernet:
    os.makedirs(APP_DIR, exist_ok=True)
    if not os.path.exists(LOCAL_KEY_FILE):
        with open(LOCAL_KEY_FILE, "wb") as f:
            f.write(Fernet.generate_key())
        try:
            os.chmod(LOCAL_KEY_FILE, 0o600)
        except (AttributeError, NotImplementedError):
            pass
    with open(LOCAL_KEY_FILE, "rb") as f:
        return Fernet(f.read())


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE, "rb") as f:
            decrypted = _get_fernet().decrypt(f.read())
        return {**DEFAULT_STATE, **json.loads(decrypted)}
    except Exception:
        return dict(DEFAULT_STATE)


def save_state(state: dict):
    os.makedirs(APP_DIR, exist_ok=True)
    encrypted = _get_fernet().encrypt(json.dumps(state).encode())
    with open(STATE_FILE, "wb") as f:
        f.write(encrypted)


# ─────────────────────────────────────────────
# VRChat API
# ─────────────────────────────────────────────
VRCHAT_API_BASE = "https://api.vrchat.cloud/api/1"
USER_AGENT = "NullforgeSimpleInviter/1.0 (contact: SaintNyklas on Discord)"

DAILY_CAP = 1000
COOLDOWN_SECONDS = 30
RECHECK_SECONDS = 35 * 60


class TwoFactorRequired(Exception):
    def __init__(self, method, partial_cookie):
        self.method = method
        self.partial_cookie = partial_cookie


def vrchat_login(username: str, password: str) -> str:
    basic = base64.b64encode(f"{username}:{password}".encode()).decode()
    resp = httpx.get(f"{VRCHAT_API_BASE}/auth/user", headers={"Authorization": f"Basic {basic}", "User-Agent": USER_AGENT}, follow_redirects=True)
    cookie = resp.cookies.get("auth", "")
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}

    requires_2fa = data.get("requiresTwoFactorAuth")
    if requires_2fa:
        method = "totp" if "totp" in requires_2fa else "emailOtp"
        raise TwoFactorRequired(method, cookie)

    if resp.status_code >= 400 or not cookie:
        raise RuntimeError(data.get("error", {}).get("message", "Login failed"))
    return cookie


def vrchat_submit_2fa(partial_cookie: str, method: str, code: str) -> str:
    endpoint = "totp" if method == "totp" else "emailotp"
    resp = httpx.post(
        f"{VRCHAT_API_BASE}/auth/twofactorauth/{endpoint}/verify",
        json={"code": code},
        headers={"Cookie": f"auth={partial_cookie}", "User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    if resp.status_code >= 400:
        raise RuntimeError("2FA code rejected")
    return partial_cookie


def vrchat_get_current_user(cookie: str) -> dict:
    resp = httpx.get(f"{VRCHAT_API_BASE}/auth/user", headers={"Cookie": f"auth={cookie}", "User-Agent": USER_AGENT}, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def vrchat_get_group(cookie: str, group_id: str) -> dict:
    resp = httpx.get(
        f"{VRCHAT_API_BASE}/groups/{group_id}",
        headers={"Cookie": f"auth={cookie}", "User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.json()


def vrchat_invite(cookie: str, group_id: str, user_id: str):
    resp = httpx.post(
        f"{VRCHAT_API_BASE}/groups/{group_id}/invites",
        json={"userId": user_id},
        headers={"Cookie": f"auth={cookie}", "User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    error_msg = ""
    if resp.status_code >= 400:
        try:
            error_msg = resp.json().get("error", {}).get("message", "")
        except Exception:
            error_msg = resp.text
    return resp.status_code, error_msg


# ─────────────────────────────────────────────
# Log watching
# ─────────────────────────────────────────────
JOIN_PATTERN = re.compile(r"OnPlayerJoined\s+(?P<name>.+?)\s+\((?P<user_id>usr_[0-9a-fA-F-]+)\)")


def find_latest_log_file(folder: str):
    candidates = glob.glob(os.path.join(folder, "output_log_*.txt"))
    return max(candidates, key=os.path.getmtime) if candidates else None


def autodetect_log_folder() -> str | None:
    """Checks VRChat's standard install location for a log folder that
    actually contains log files, so setup works with zero clicks when
    VRChat is installed in the default spot."""
    candidates = [
        os.path.join(os.environ.get("APPDATA", ""), "..", "LocalLow", "VRChat", "VRChat"),
        os.path.join(os.path.expanduser("~"), "AppData", "LocalLow", "VRChat", "VRChat"),
    ]
    for path in candidates:
        path = os.path.normpath(path)
        if os.path.isdir(path) and find_latest_log_file(path):
            return path
    return None


def tail_file(path: str, stop_event: threading.Event):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(0, os.SEEK_END)
        while not stop_event.is_set():
            line = f.readline()
            if not line:
                time.sleep(0.5)
                continue
            yield line


# ─────────────────────────────────────────────
# JS-facing API — every method here is callable from ui/index.html as
# `await pywebview.api.<method_name>(...)`
# ─────────────────────────────────────────────
class Api:
    def __init__(self):
        self.state = load_state()
        self.detected = []  # [{"name":..., "user_id":..., "invited": bool}]
        self.activity = []  # list[str], newest last
        self.watching_file = None
        self.pending_2fa = None  # {"partial_cookie":..., "method":...}
        self.stop_event = threading.Event()
        self.watch_thread = None

        if not self.state["vrchat_log_folder"]:
            detected = autodetect_log_folder()
            if detected:
                self.state["vrchat_log_folder"] = detected
                save_state(self.state)
                self._log(f"Auto-detected VRChat log folder: {detected}")

        if self.state["vrchat_log_folder"]:
            self._restart_watcher()

        if self.state["group_id"] and not self.state["group_name"] and self.state["auth_cookie"]:
            self._resolve_group_name()

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.activity.append(f"[{ts}] {msg}")
        self.activity = self.activity[-300:]  # cap so it doesn't grow forever

    # ---------- called by JS on every refresh tick ----------
    def get_state(self):
        return {
            "vrchat_log_folder": self.state["vrchat_log_folder"],
            "group_id": self.state["group_id"],
            "group_name": self.state["group_name"],
            "auth_ok": bool(self.state["auth_cookie"]),
            "auth_display": self.state["auth_display_name"] or "Not logged in",
            "auto_invite_enabled": self.state["auto_invite_enabled"],
            "invite_delay_seconds": self.state["invite_delay_seconds"],
            "watching_file": self.watching_file,
            "total_invited": len(self.state["invited_user_ids"]),
            "detected": self.detected[-500:],  # cap what's sent/rendered
            "activity": self.activity,
        }

    # ---------- setup ----------
    def pick_log_folder(self):
        detected = autodetect_log_folder()
        default = detected or os.path.expanduser("~")
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, directory=default)
        if result:
            folder = result[0]
            self.state["vrchat_log_folder"] = folder
            save_state(self.state)
            self._log(f"Log folder set to {folder}")
            self._restart_watcher()

    def set_group(self, group_id: str):
        self.state["group_id"] = group_id.strip()
        self.state["group_name"] = ""
        save_state(self.state)
        self._log(f"Group set to {self.state['group_id']}")
        self._resolve_group_name()

    def _resolve_group_name(self):
        if not self.state["group_id"] or not self.state["auth_cookie"]:
            return
        try:
            group = vrchat_get_group(self.state["auth_cookie"], self.state["group_id"])
            self.state["group_name"] = group.get("name", "")
            save_state(self.state)
        except Exception as e:
            self._log(f"Could not fetch group name: {e}")

    def login(self, username: str, password: str):
        try:
            cookie = vrchat_login(username, password)
        except TwoFactorRequired as tfa:
            self.pending_2fa = {"partial_cookie": tfa.partial_cookie, "method": tfa.method}
            return {"needs_2fa": True, "method": tfa.method}
        except Exception as e:
            return {"error": str(e)}

        return self._finish_login(cookie)

    def login_with_cookie(self, cookie: str):
        cookie = cookie.strip()
        if not cookie:
            return {"error": "No cookie entered."}
        # VRChat cookies are sometimes copied including the "auth=" prefix or
        # surrounding quotes — strip those so a raw paste still works.
        cookie = cookie.removeprefix("auth=").strip('"').strip("'")
        return self._finish_login(cookie)

    def submit_2fa(self, code: str):
        if not self.pending_2fa:
            return {"error": "No pending 2FA challenge."}
        try:
            cookie = vrchat_submit_2fa(self.pending_2fa["partial_cookie"], self.pending_2fa["method"], code)
        except Exception as e:
            return {"error": str(e)}
        finally:
            self.pending_2fa = None
        return self._finish_login(cookie)

    def _finish_login(self, cookie: str):
        try:
            user = vrchat_get_current_user(cookie)
        except Exception as e:
            return {"error": f"Login succeeded but fetching profile failed: {e}"}
        self.state["auth_cookie"] = cookie
        self.state["auth_display_name"] = user.get("displayName", "?")
        save_state(self.state)
        self._log(f"Logged in as {self.state['auth_display_name']}.")
        self._resolve_group_name()
        return {"success": True}

    def toggle_auto_invite(self, value: bool):
        self.state["auto_invite_enabled"] = bool(value)
        save_state(self.state)

    def set_delay(self, seconds: int):
        self.state["invite_delay_seconds"] = max(0, int(seconds))
        save_state(self.state)

    # ---------- detected list actions ----------
    def invite_selected(self, user_ids: list):
        for user_id in user_ids:
            entry = next((d for d in self.detected if d["user_id"] == user_id), None)
            if entry:
                self._dispatch_invite(entry["name"], user_id)

    def mark_invited(self, user_ids: list):
        for user_id in user_ids:
            if user_id not in self.state["invited_user_ids"]:
                self.state["invited_user_ids"].append(user_id)
            for d in self.detected:
                if d["user_id"] == user_id:
                    d["invited"] = True
        save_state(self.state)

    def clear_list(self):
        self.detected = []

    def save_list(self):
        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename="nullforge_detected_list.json",
        )
        if not result:
            return {"saved": False}
        path = result if isinstance(result, str) else result[0]
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.detected, f, indent=2)
            self._log(f"Saved {len(self.detected)} entries to {os.path.basename(path)}.")
            return {"saved": True, "path": path}
        except Exception as e:
            self._log(f"Failed to save list: {e}")
            return {"saved": False, "error": str(e)}

    def load_list(self):
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        if not result:
            return {"loaded": False}
        path = result[0]
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, list):
                raise ValueError("File does not contain a list.")

            existing_ids = {d["user_id"] for d in self.detected}
            added = 0
            for entry in loaded:
                if not isinstance(entry, dict) or "user_id" not in entry or "name" not in entry:
                    continue
                if entry["user_id"] in existing_ids:
                    continue
                self.detected.append({
                    "name": entry["name"],
                    "user_id": entry["user_id"],
                    "invited": bool(entry.get("invited", False)),
                })
                existing_ids.add(entry["user_id"])
                added += 1

            self._log(f"Loaded {added} new entries from {os.path.basename(path)} (skipped duplicates).")
            return {"loaded": True, "added": added}
        except Exception as e:
            self._log(f"Failed to load list: {e}")
            return {"loaded": False, "error": str(e)}

    # ---------- invite dispatch with local rate-limit handling ----------
    def _is_rate_limited(self) -> bool:
        until = self.state.get("rate_limited_until")
        return bool(until and datetime.now(timezone.utc) < datetime.fromisoformat(until))

    def _reset_daily_if_needed(self):
        reset_at = self.state.get("daily_count_reset_at")
        if not reset_at or datetime.now(timezone.utc) - datetime.fromisoformat(reset_at) >= timedelta(hours=24):
            self.state["daily_invite_count"] = 0
            self.state["daily_count_reset_at"] = datetime.now(timezone.utc).isoformat()

    def _dispatch_invite(self, name: str, user_id: str):
        if not self.state["auth_cookie"] or not self.state["group_id"]:
            self._log("Cannot invite — not logged in or no group set.")
            return
        if user_id in self.state["invited_user_ids"]:
            return

        self._reset_daily_if_needed()
        if self._is_rate_limited():
            self._log(f"Currently rate-limited — skipping {name}.")
            return
        if self.state["daily_invite_count"] >= DAILY_CAP:
            self._log(f"Daily cap ({DAILY_CAP}) reached — stopping until reset.")
            self.state["rate_limited_until"] = (datetime.now(timezone.utc) + timedelta(seconds=RECHECK_SECONDS)).isoformat()
            save_state(self.state)
            return

        status_code, error_msg = vrchat_invite(self.state["auth_cookie"], self.state["group_id"], user_id)

        if status_code < 400:
            self.state["invited_user_ids"].append(user_id)
            self.state["daily_invite_count"] += 1
            self._log(f"Invited {name}.")
            for d in self.detected:
                if d["user_id"] == user_id:
                    d["invited"] = True
        else:
            msg = (error_msg or "").lower()
            if status_code == 429 or "cooldown" in msg or "too many" in msg:
                self.state["rate_limited_until"] = (datetime.now(timezone.utc) + timedelta(seconds=COOLDOWN_SECONDS)).isoformat()
                self._log(f"Cooldown hit inviting {name} — pausing {COOLDOWN_SECONDS}s.")
            elif "already" in msg:
                self.state["invited_user_ids"].append(user_id)
                self._log(f"{name}: {error_msg}")
            else:
                self._log(f"Failed to invite {name}: {error_msg or status_code}")

        save_state(self.state)

    # ---------- log watching ----------
    def _restart_watcher(self):
        self.stop_event.set()
        if self.watch_thread:
            self.watch_thread.join(timeout=2)
        self.stop_event = threading.Event()
        self.watch_thread = threading.Thread(target=self._watch_loop, args=(self.stop_event,), daemon=True)
        self.watch_thread.start()

    def _watch_loop(self, stop_event: threading.Event):
        folder = self.state["vrchat_log_folder"]
        if not folder or not os.path.isdir(folder):
            self._log("Watcher stopped: log folder missing or not set.")
            return

        path = find_latest_log_file(folder)
        if not path:
            self._log("No output_log file found yet — will keep checking every 5s.")
            while not path and not stop_event.is_set():
                time.sleep(5)
                path = find_latest_log_file(folder)
            if stop_event.is_set():
                return
            self._log(f"Found log file: {os.path.basename(path)}")

        self.watching_file = os.path.basename(path)
        self._log(f"Watching: {self.watching_file}")

        for line in tail_file(path, stop_event):
            if stop_event.is_set():
                return
            latest = find_latest_log_file(folder)
            if latest and latest != path:
                self._log(f"Log rotated — switching to {os.path.basename(latest)}")
                return self._watch_loop(stop_event)

            m = JOIN_PATTERN.search(line)
            if not m:
                continue
            name, user_id = m.group("name").strip(), m.group("user_id")
            already_invited = user_id in self.state["invited_user_ids"]
            self.detected.append({"name": name, "user_id": user_id, "invited": already_invited})
            self._log(f"Detected join: {name}")

            if not self.state["auto_invite_enabled"] or already_invited:
                continue

            delay = self.state["invite_delay_seconds"]
            if delay > 0:
                threading.Timer(delay, lambda n=name, u=user_id: self._dispatch_invite(n, u)).start()
            else:
                self._dispatch_invite(name, user_id)


def main():
    api = Api()
    webview.create_window(
        "Nullforge Simple Inviter",
        UI_INDEX,
        js_api=api,
        width=1100,
        height=760,
        background_color="#0b0710",
    )
    webview.start()


if __name__ == "__main__":
    main()
