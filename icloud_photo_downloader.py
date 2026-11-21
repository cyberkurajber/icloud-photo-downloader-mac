#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iCloud Photo Downloader -- a simple GUI.

A small Tkinter front-end around `icloudpd`
(https://github.com/icloud-photos-downloader/icloud_photos_downloader).

Why this design:
  * READ-ONLY ON iCLOUD. This app NEVER passes any delete/move flag to icloudpd
    (no --auto-delete, --delete-after-download, --keep-icloud-recent-days). It only
    ever downloads. Nothing in iCloud is modified or removed.
  * RESUMABLE / NO OVERWRITE. icloudpd's default "copy" mode skips assets that are
    already present on disk, so an interrupted transfer simply continues where it
    stopped the next time you press Start. Files already downloaded are not
    re-downloaded and never overwritten.
  * NO 1000-PHOTO LIMIT. The 1000-item cap on iCloud.com is a limit of the website's
    download button only. icloudpd uses the iCloud API and pulls the whole library.
  * WORKS ON macOS 10.13 HIGH SIERRA with python.org Python 3.9+ (Tcl/Tk 8.6).

icloudpd is launched inside a pseudo-terminal (pty) so the password can be typed at
its prompt (never on the command line) and so its tqdm progress bar stays active,
giving an exact "downloaded / total" count to drive the GUI.
"""

import os
import sys
import re
import json
import shlex
import shutil
import signal
import struct
import threading
import queue
import time
import datetime
import subprocess

try:
    import pty
    import fcntl
    import termios
    _HAVE_PTY = True
except ImportError:  # pragma: no cover
    _HAVE_PTY = False

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog, scrolledtext


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
APP_TITLE = "iCloud Photo Downloader"
APP_DIR = os.path.expanduser("~/.icloud_photo_downloader")
SESSION_DIR = os.path.join(APP_DIR, "session")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

FORBIDDEN_FLAGS = (
    "--auto-delete",
    "--delete-after-download",
    "--keep-icloud-recent-days",
)


# --------------------------------------------------------------------------- #
# Output parsing (pure functions, unit-testable headlessly)
# --------------------------------------------------------------------------- #
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

TQDM_FRAC_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*\[")
TQDM_PCT_RE = re.compile(r"(\d+)\s*%\s*\|")
TQDM_RATE_RE = re.compile(r"([\d.]+)\s*it/s")
TQDM_SPI_RE = re.compile(r"([\d.]+)\s*s/it")   # slow items (big videos): seconds per item
TQDM_ETA_RE = re.compile(r"<\s*([0-9:]+)\s*,")

TOTAL_RE = re.compile(r"Downloading\s+(\d+)\b.*?\bto\b.*?\.\.\.")
DOWNLOADED_RE = re.compile(r"\bDownloaded\s+(\S.*?)\s*$")
ALREADY_RE = re.compile(r"already exists", re.I)
CURRENT_RE = re.compile(r"\bDownloading\s+(?!\d)(\S.*?)\.\.\.\s*$")
LOOKING_RE = re.compile(r"Looking up all", re.I)
DONE_RE = re.compile(r"have been downloaded", re.I)

# Only the actual input prompts (icloudpd 1.34: "iCloud Password for <user>:",
# "Please enter two-factor authentication code or device index (a..b) ...",
# "Please enter two-factor authentication code that you received over SMS").
# Informational lines such as "Two-factor authentication is required (2fa)" or
# "Bad username or password for <user>" must NOT match.
PASSWORD_RE = re.compile(r"(?:^|\s)(?:Enter\s+)?iCloud password for \S+:\s*$", re.I)
MFA_RE = re.compile(
    r"Please enter (?:two-factor|two-step) authentication code|Please enter validation code",
    re.I,
)
MFA_DEVICE_RE = re.compile(r"^\s+([a-z0-9]):\s+(\S.*?)\s*$")   # "  a: (•••) •••-••12"
MFA_RETRY_RE = re.compile(r"Try again\s*$", re.I)
SECURITY_KEY_RE = re.compile(r"Plug in a security key", re.I)
ADP_APPROVE_RE = re.compile(r"Approve the iCloud web access request", re.I)
ERROR_LINE_RE = re.compile(r"\bERROR\b")

_WRONG_PW_MSG = ("Apple rejected the Apple ID or password. Check the credentials and retry. "
                 "If the password is right, press 'Sign out' (clears the saved session) "
                 "and sign in again.")

# (pattern, message). message may be a callable taking the regex match.
ERROR_PATTERNS = [
    # Must come before the wrong-password pattern: Apple's lock message arrives on
    # the same line as "Invalid email/password combination".
    (re.compile(r"\(-20209\)|account has been\W+locked", re.I),
     "Apple has LOCKED this Apple Account for security reasons (often after several "
     "sign-in attempts). The password is not the problem. Unlock it at "
     "https://iforgot.apple.com or on the iPhone (Settings > [name] > Sign-In & "
     "Security), wait a while, then press 'Sign out' and try again. Do NOT keep "
     "retrying -- each attempt can extend the lock."),
    (re.compile(r"Web access to iCloud data is disabled", re.I),
     "Web access is disabled for this account. On the iPhone/iPad turn ON "
     "Settings > [name] > iCloud > 'Access iCloud Data on the Web', then try again."),
    (re.compile(r"Timed out waiting for the iCloud web access approval", re.I),
     "The web-access request was not approved in time. Run again and tap Allow "
     "on the Apple device when it asks."),
    (re.compile(r"ACCESS_DENIED", re.I),
     "Apple returned ACCESS_DENIED. On the iCloud account, turn ON "
     "'Access iCloud Data on the Web'. If Advanced Data Protection is ON, either "
     "turn it off or tick 'Account uses Advanced Data Protection' and approve "
     "the request on the Apple device. Then try again."),
    (re.compile(r"\(-20101\)|Bad username or password|invalid email/password", re.I),
     _WRONG_PW_MSG),
    (re.compile(r"Failed to verify (?:the security key|two-factor authentication code|"
                r"two-step authentication code)", re.I),
     "The verification code (or security key) was rejected. Try again with a fresh code."),
    (re.compile(r"Failed to send two-factor authentication code", re.I),
     "Apple could not send the SMS code. Try again, or use the code shown on a "
     "trusted Apple device."),
    (re.compile(r"has not finished indexing", re.I),
     "iCloud is still preparing (indexing) this photo library. This can take hours "
     "after turning on web access or after a large upload. Please try again later."),
    (re.compile(r"saved session is missing or no longer valid", re.I),
     "The saved session expired. Use 'Sign in' to re-authenticate, then Start."),
    (re.compile(r"Apple says: \"(.+?)\"", re.I),
     lambda m: "Apple refused the sign-in: %s" % m.group(1)),
]


def strip_ansi(text):
    return ANSI_RE.sub("", text)


def looks_like_tqdm(seg):
    return ("%|" in seg) or ("it/s" in seg)


def parse_tqdm(seg):
    if not looks_like_tqdm(seg):
        return None
    out = {"cur": None, "total": None, "pct": None, "rate": None, "eta": None}
    m = TQDM_FRAC_RE.search(seg)
    if m:
        out["cur"] = int(m.group(1))
        out["total"] = int(m.group(2))
    m = TQDM_PCT_RE.search(seg)
    if m:
        out["pct"] = int(m.group(1))
    m = TQDM_RATE_RE.search(seg)
    if m:
        out["rate"] = float(m.group(1))
    else:
        m = TQDM_SPI_RE.search(seg)
        if m and float(m.group(1)) > 0:
            out["rate"] = 1.0 / float(m.group(1))
    m = TQDM_ETA_RE.search(seg)
    if m:
        out["eta"] = m.group(1)
    if out["cur"] is None and out["pct"] is None:
        return None
    return out


def classify_log(seg):
    s = seg.strip()
    if not s:
        return (None, None)
    if PASSWORD_RE.search(s):
        return ("password", None)
    if MFA_RE.search(s):
        return ("mfa", None)
    if SECURITY_KEY_RE.search(s):
        return ("security_key", None)
    if ADP_APPROVE_RE.search(s):
        return ("adp_approve", None)
    for pat, msg in ERROR_PATTERNS:
        m = pat.search(s)
        if m:
            return ("error", msg(m) if callable(msg) else msg)
    if MFA_RETRY_RE.search(s):
        return ("mfa_retry", None)
    m = TOTAL_RE.search(s)
    if m:
        return ("total", int(m.group(1)))
    if DOWNLOADED_RE.search(s):
        return ("downloaded", DOWNLOADED_RE.search(s).group(1))
    if ALREADY_RE.search(s):
        return ("skipped", None)
    if LOOKING_RE.search(s):
        return ("looking", None)
    if DONE_RE.search(s):
        return ("done", None)
    m = CURRENT_RE.search(s)
    if m:
        return ("current", m.group(1))
    return (None, None)


def find_icloudpd():
    candidate = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "icloudpd")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which("icloudpd")


KEYCHAIN_SERVICE = "pyicloud://icloud-password"   # where icloudpd's keyring saves passwords


def remove_keychain_passwords(limit=200):
    """Delete every iCloud password icloudpd stored in the login Keychain.
    Returns how many were removed. Safe to call when there are none."""
    removed = 0
    if not shutil.which("security"):
        return 0
    for _ in range(limit):
        try:
            r = subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except Exception:
            break
        if r.returncode != 0:
            break
        removed += 1
    return removed


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


# --------------------------------------------------------------------------- #
# Subprocess runner (icloudpd inside a pty, on a background thread)
# --------------------------------------------------------------------------- #
class IcloudpdRunner(threading.Thread):
    def __init__(self, argv, password, out_queue, mfa_callback, keep_awake=False):
        super().__init__(daemon=True)
        self.argv = argv
        self.keep_awake = keep_awake
        self._caffeinate = None
        self.password = password or ""
        self.q = out_queue
        self.mfa_callback = mfa_callback
        self.proc = None
        self._master = None
        self._stop_requested = False
        self._password_sent = False
        self._mfa_prompts_answered = set()   # prompt texts already answered
        self._mfa_devices = []               # ("a", "(•••) •••-••12") for SMS fallback

    def stop(self):
        self._stop_requested = True
        proc = self.proc
        if not proc:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGINT)
            else:
                proc.send_signal(signal.SIGINT)
        except Exception:
            pass

    def _emit(self, **event):
        self.q.put(event)

    def _write_child(self, text):
        if self._master is not None:
            try:
                os.write(self._master, text.encode("utf-8"))
            except OSError:
                pass

    def _handle(self, seg, allow_counters):
        seg = strip_ansi(seg)
        if not seg.strip():
            return

        if allow_counters:
            dm = MFA_DEVICE_RE.match(seg)
            if dm:
                self._mfa_devices.append((dm.group(1), dm.group(2)))
                self._emit(type="log", line=seg.rstrip())
                return

        tq = parse_tqdm(seg)
        if tq is not None:
            self._emit(type="progress", **tq)
            self._emit(type="status", value="Downloading")
            return

        kind, payload = classify_log(seg)

        if kind in ("password", "mfa") and self._stop_requested:
            return   # e.g. a traceback quoting the prompt after Stop/cancel
        if kind == "password":
            if not self._password_sent:
                self._password_sent = True
                self._emit(type="status", value="Signing in...")
                self._write_child(self.password + "\n")
            return
        if kind == "mfa":
            # The same prompt arrives twice: once as the open prompt and once more
            # (with the typed answer echoed) when the line completes. Answer once per
            # prompt; a "Try again" line clears the memory so a re-prompt is answered.
            key = seg.strip().split(": ")[0].rstrip(": ").strip()
            if key not in self._mfa_prompts_answered:
                self._mfa_prompts_answered.add(key)
                self._emit(type="status", value="Waiting for 2FA code...")
                sms = "received over sms" in key.lower()
                code = self.mfa_callback(sms, list(self._mfa_devices)) or ""
                if not code:
                    self._emit(type="log", line="2FA cancelled -- stopping.")
                    self.stop()
                    return
                self._emit(type="status", value="Verifying code...")
                self._write_child(code + "\n")
            return

        if not allow_counters:
            if kind == "looking":
                self._emit(type="status", value="Scanning iCloud library...")
            return

        if kind == "mfa_retry":
            self._mfa_prompts_answered.clear()
        elif kind == "security_key":
            self._emit(type="status", value="Touch your security key (YubiKey) now...")
            self._emit(type="info", msg="This account uses a security key instead of "
                       "codes. Plug in the security key and touch it now.")
        elif kind == "adp_approve":
            self._emit(type="status", value="Approve the request on your Apple device...")
            self._emit(type="info", msg="Advanced Data Protection: approve the iCloud "
                       "web-access request that just appeared on your iPhone/iPad/Mac.")
        elif kind == "error":
            self._emit(type="error", msg=payload)
        elif kind == "total":
            self._emit(type="total", value=payload)
        elif kind == "downloaded":
            self._emit(type="downloaded", file=payload)
        elif kind == "skipped":
            self._emit(type="skipped")
        elif kind == "current":
            self._emit(type="current", file=payload)
        elif kind == "looking":
            self._emit(type="status", value="Scanning iCloud library...")
        elif kind == "done":
            self._emit(type="status", value="Finalizing...")

        if ERROR_LINE_RE.search(seg):
            self._emit(type="error_line", line=seg.strip())
        self._emit(type="log", line=seg.rstrip())

    def run(self):
        if not _HAVE_PTY:
            self._emit(type="error", msg="This app requires macOS/Linux (pty support).")
            self._emit(type="finished", code=1)
            return

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["TERM"] = "xterm"
        env["COLUMNS"] = "120"
        env["LINES"] = "40"

        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        except Exception:
            pass

        try:
            self.proc = subprocess.Popen(
                self.argv,
                stdin=slave, stdout=slave, stderr=slave,
                env=env, close_fds=True, preexec_fn=os.setsid,
            )
        except FileNotFoundError:
            os.close(master); os.close(slave)
            self._emit(type="error", msg="Could not launch icloudpd. Run install.sh first.")
            self._emit(type="finished", code=127)
            return
        except Exception as exc:  # pragma: no cover
            os.close(master); os.close(slave)
            self._emit(type="error", msg="Failed to start icloudpd: %s" % exc)
            self._emit(type="finished", code=1)
            return

        os.close(slave)
        self._master = master

        # Keep the Mac from sleeping while icloudpd runs (-i idle sleep, -s system
        # sleep on AC power). "-w PID" makes caffeinate exit by itself with icloudpd.
        if self.keep_awake and shutil.which("caffeinate"):
            try:
                self._caffeinate = subprocess.Popen(
                    ["caffeinate", "-i", "-s", "-w", str(self.proc.pid)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
            except Exception:
                self._caffeinate = None

        buf = ""
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            buf += data.decode("utf-8", "replace")
            parts = re.split(r"[\r\n]", buf)
            pending = parts[-1]
            for seg in parts[:-1]:
                self._handle(seg, allow_counters=True)
            self._handle(pending, allow_counters=False)
            buf = pending

        try:
            os.close(master)
        except OSError:
            pass

        code = self.proc.wait()
        if self._caffeinate is not None and self._caffeinate.poll() is None:
            try:
                self._caffeinate.terminate()
            except Exception:
                pass
        if self._stop_requested:
            self._emit(type="status", value="Stopped")
        self._emit(type="finished", code=code)


# --------------------------------------------------------------------------- #
# The application window
# --------------------------------------------------------------------------- #
class App:
    POLL_MS = 120

    FOLDER_CHOICES = [
        ("By month  (e.g. 2024/06/)", "{:%Y/%m}"),
        ("By day  (e.g. 2024/06/15/)", "{:%Y/%m/%d}"),
        ("By year  (e.g. 2024/)", "{:%Y}"),
        ("All in one folder", "none"),
    ]

    SAVED_KEYS = ("email", "dir", "folder", "skip_videos", "skip_live", "exif",
                  "save_log", "quick", "until_n", "album", "date_from", "date_to",
                  "sound", "adp", "keep_awake", "auto_retry")

    # Auto-retry after a failed download run (connection drop, Apple hiccup).
    RETRY_MAX = 10                                # attempts in a row without progress
    RETRY_DELAYS = (60, 120, 300)                 # seconds; the last value repeats

    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.minsize(720, 800)

        self.runner = None
        self.q = queue.Queue()

        self.total = 0
        self.cur = 0
        self.downloaded = 0
        self.skipped = 0
        self.start_time = None
        self.mode = None            # "download" | "preview" | "test" | "albums"
        self.logfile = None
        self.logfile_path = None

        self._mfa_result = None
        self._mfa_event = None

        self._job = None                # (argv, pw, mode, dest) of the last launch
        self._retry_job = None          # Tk after() id while waiting to retry
        self._retry_at = None
        self._retry_attempt = 0
        self._downloaded_at_launch = 0
        self._errors_shown = set()
        self._last_error_line = None

        self._settings = self._load_settings()
        self._build_ui()
        self._apply_settings_to_ui()
        self._update_free_space()

        self.root.after(self.POLL_MS, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.update_idletasks()
        self.root.after(60, self._force_redraw)

    def _force_redraw(self):
        try:
            self.root.update_idletasks()
            w = self.root.winfo_width(); h = self.root.winfo_height()
            if w > 1 and h > 1:
                self.root.geometry("%dx%d" % (w + 1, h + 1))
                self.root.update_idletasks()
                self.root.geometry("%dx%d" % (w, h))
            self.root.update_idletasks()
        except Exception:
            pass

    # -- settings ----------------------------------------------------------- #
    def _load_settings(self):
        try:
            with open(SETTINGS_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_settings(self):
        data = {
            "email": self.email_var.get(),
            "dir": self.dir_var.get(),
            "folder": self.folder_var.get(),
            "skip_videos": self.skip_videos_var.get(),
            "skip_live": self.skip_live_var.get(),
            "exif": self.exif_var.get(),
            "save_log": self.save_log_var.get(),
            "quick": self.quick_var.get(),
            "until_n": self.until_n_var.get(),
            "album": self.album_var.get(),
            "date_from": self.from_var.get(),
            "date_to": self.to_var.get(),
            "sound": self.sound_var.get(),
            "adp": self.adp_var.get(),
            "keep_awake": self.keep_awake_var.get(),
            "auto_retry": self.retry_var.get(),
        }
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _apply_settings_to_ui(self):
        s = self._settings
        if s.get("email"):
            self.email_var.set(s["email"])
        if s.get("dir"):
            self.dir_var.set(s["dir"])
        if s.get("folder") in [c[0] for c in self.FOLDER_CHOICES]:
            self.folder_var.set(s["folder"])
        self.skip_videos_var.set(bool(s.get("skip_videos", False)))
        self.skip_live_var.set(bool(s.get("skip_live", False)))
        self.exif_var.set(bool(s.get("exif", False)))
        self.save_log_var.set(bool(s.get("save_log", True)))
        self.quick_var.set(bool(s.get("quick", False)))
        if s.get("until_n"):
            self.until_n_var.set(str(s["until_n"]))
        if s.get("album"):
            self.album_var.set(s["album"])
        if s.get("date_from"):
            self.from_var.set(s["date_from"])
        if s.get("date_to"):
            self.to_var.set(s["date_to"])
        self.sound_var.set(bool(s.get("sound", True)))
        self.adp_var.set(bool(s.get("adp", False)))
        self.keep_awake_var.set(bool(s.get("keep_awake", True)))
        self.retry_var.set(bool(s.get("auto_retry", True)))

    # -- UI ----------------------------------------------------------------- #
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Apple ID (email):").grid(row=0, column=0, sticky="w", **pad)
        self.email_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.email_var).grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(frm, text="Password:").grid(row=1, column=0, sticky="w", **pad)
        self.pw_var = tk.StringVar()
        self.pw_entry = ttk.Entry(frm, textvariable=self.pw_var, show="•")
        self.pw_entry.grid(row=1, column=1, sticky="ew", **pad)
        self.show_pw_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Show", variable=self.show_pw_var,
                        command=self._toggle_pw).grid(row=1, column=2, sticky="w", **pad)

        ttk.Label(frm, text="Save photos to:").grid(row=2, column=0, sticky="w", **pad)
        self.dir_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.dir_var).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse...", command=self._browse).grid(row=2, column=2, sticky="ew", **pad)

        self.free_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.free_var, foreground="#666").grid(
            row=3, column=1, columnspan=2, sticky="w", padx=8)

        # ---- Options ----
        opt = ttk.LabelFrame(frm, text="Options", padding=8)
        opt.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        opt.columnconfigure(1, weight=1)
        opt.columnconfigure(3, weight=1)
        op = {"padx": 4, "pady": 2}

        self.skip_videos_var = tk.BooleanVar(value=False)
        self.skip_live_var = tk.BooleanVar(value=False)
        self.exif_var = tk.BooleanVar(value=False)
        self.save_log_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Photos only (skip videos)",
                        variable=self.skip_videos_var).grid(row=0, column=0, columnspan=2, sticky="w", **op)
        ttk.Checkbutton(opt, text="Skip Live Photo videos",
                        variable=self.skip_live_var).grid(row=0, column=2, columnspan=2, sticky="w", **op)
        ttk.Checkbutton(opt, text="Set photo date from EXIF (if missing)",
                        variable=self.exif_var).grid(row=1, column=0, columnspan=2, sticky="w", **op)
        ttk.Checkbutton(opt, text="Save a log file in the destination",
                        variable=self.save_log_var).grid(row=1, column=2, columnspan=2, sticky="w", **op)

        ttk.Label(opt, text="Folder organization:").grid(row=2, column=0, sticky="w", **op)
        self.folder_var = tk.StringVar(value=self.FOLDER_CHOICES[0][0])
        ttk.Combobox(opt, textvariable=self.folder_var, state="readonly",
                     values=[c[0] for c in self.FOLDER_CHOICES]).grid(
                         row=2, column=1, columnspan=3, sticky="ew", **op)

        # Quick update
        self.quick_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Quick update (incremental re-sync)",
                        variable=self.quick_var).grid(row=3, column=0, columnspan=2, sticky="w", **op)
        qf = ttk.Frame(opt)
        qf.grid(row=3, column=2, columnspan=2, sticky="w", **op)
        ttk.Label(qf, text="stop after").pack(side="left")
        self.until_n_var = tk.StringVar(value="100")
        ttk.Entry(qf, textvariable=self.until_n_var, width=5).pack(side="left", padx=4)
        ttk.Label(qf, text="already-downloaded in a row").pack(side="left")

        # Album
        ttk.Label(opt, text="Album (empty = all):").grid(row=4, column=0, sticky="w", **op)
        self.album_var = tk.StringVar()
        ttk.Entry(opt, textvariable=self.album_var).grid(row=4, column=1, columnspan=2, sticky="ew", **op)
        ttk.Button(opt, text="List albums", command=self._on_list_albums).grid(row=4, column=3, sticky="ew", **op)

        # Date range
        ttk.Label(opt, text="From (YYYY-MM-DD):").grid(row=5, column=0, sticky="w", **op)
        self.from_var = tk.StringVar()
        ttk.Entry(opt, textvariable=self.from_var).grid(row=5, column=1, sticky="ew", **op)
        ttk.Label(opt, text="To (YYYY-MM-DD):").grid(row=5, column=2, sticky="e", **op)
        self.to_var = tk.StringVar()
        ttk.Entry(opt, textvariable=self.to_var).grid(row=5, column=3, sticky="ew", **op)

        # Advanced + sound
        ttk.Label(opt, text="Advanced icloudpd options:").grid(row=6, column=0, sticky="w", **op)
        self.extra_var = tk.StringVar()
        ttk.Entry(opt, textvariable=self.extra_var).grid(row=6, column=1, columnspan=3, sticky="ew", **op)
        self.sound_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Play a sound / notify when finished",
                        variable=self.sound_var).grid(row=7, column=0, columnspan=2, sticky="w", **op)
        self.adp_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Account uses Advanced Data Protection",
                        variable=self.adp_var).grid(row=7, column=2, columnspan=2, sticky="w", **op)
        self.keep_awake_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Keep Mac awake while running",
                        variable=self.keep_awake_var).grid(row=8, column=0, columnspan=2, sticky="w", **op)
        self.retry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Auto-retry if the download stops with an error",
                        variable=self.retry_var).grid(row=8, column=2, columnspan=2, sticky="w", **op)

        # ---- Buttons ----
        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
        self.test_btn = ttk.Button(btns, text="Sign in", command=self._on_test)
        self.test_btn.pack(side="left", padx=4)
        self.preview_btn = ttk.Button(btns, text="Preview (count)", command=self._on_preview)
        self.preview_btn.pack(side="left", padx=4)
        self.start_btn = ttk.Button(btns, text="Start download", command=self._on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.signout_btn = ttk.Button(btns, text="Sign out", command=self._on_signout)
        self.signout_btn.pack(side="left", padx=4)

        # ---- Progress ----
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(frm, textvariable=self.status_var, font=("", 12, "bold")).grid(
            row=6, column=0, columnspan=3, sticky="w", **pad)
        self.pbar = ttk.Progressbar(frm, mode="determinate", maximum=100)
        self.pbar.grid(row=7, column=0, columnspan=3, sticky="ew", **pad)
        self.stats_var = tk.StringVar(
            value="Total: -   Downloaded: 0   Already on disk: 0   Remaining: -")
        ttk.Label(frm, textvariable=self.stats_var).grid(row=8, column=0, columnspan=3, sticky="w", **pad)
        self.meta_var = tk.StringVar(value="Speed: -   ETA: -   Elapsed: 0s")
        ttk.Label(frm, textvariable=self.meta_var).grid(row=9, column=0, columnspan=3, sticky="w", **pad)
        self.current_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.current_var, foreground="#555").grid(
            row=10, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(frm, text="Activity log:").grid(row=11, column=0, sticky="w", padx=8)
        self.log = scrolledtext.ScrolledText(frm, height=12, wrap="none", state="disabled")
        self.log.grid(row=12, column=0, columnspan=3, sticky="nsew", padx=8, pady=4)
        frm.rowconfigure(12, weight=1)

    def _toggle_pw(self):
        self.pw_entry.configure(show="" if self.show_pw_var.get() else "•")

    def _browse(self):
        d = filedialog.askdirectory(title="Choose the destination folder")
        if d:
            self.dir_var.set(d)
            self._update_free_space()

    def _update_free_space(self):
        d = self.dir_var.get().strip()
        if d and os.path.isdir(d):
            try:
                free = shutil.disk_usage(d).free
                self.free_var.set("Free space on this drive: %s" % human_bytes(free))
                return
            except Exception:
                pass
        self.free_var.set("")

    def _log_line(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        try:
            count = int(self.log.index("end-1c").split(".")[0])
            if count > 600:
                self.log.delete("1.0", "%d.0" % (count - 600))
        except Exception:
            pass
        self.log.see("end")
        self.log.configure(state="disabled")
        if self.logfile:
            try:
                self.logfile.write(line + "\n")
                self.logfile.flush()
            except Exception:
                pass

    # -- validation & argument building ------------------------------------- #
    def _validate(self, need_dir):
        email = self.email_var.get().strip()
        if "@" not in email:
            messagebox.showerror(APP_TITLE, "Please enter a valid Apple ID email address.")
            return None
        if not self.pw_var.get():
            messagebox.showerror(APP_TITLE, "Please enter the iCloud password.")
            return None
        dest = self.dir_var.get().strip()
        if need_dir:
            if not dest:
                messagebox.showerror(APP_TITLE, "Please choose a destination folder.")
                return None
            if not os.path.isdir(dest):
                messagebox.showerror(APP_TITLE, "The destination folder does not exist.")
                return None
            if not os.access(dest, os.W_OK):
                messagebox.showerror(APP_TITLE, "The destination folder is not writable.")
                return None
        return email, self.pw_var.get(), dest

    def _common_args(self, email):
        # "--password-provider console": take the password only from this app (typed
        # into icloudpd's prompt). icloudpd's default also READS and WRITES the macOS
        # Keychain, which would keep every customer's iCloud password on this Mac and
        # could retry a stale saved password (a fast way to get an account locked).
        args = ["--username", email, "--cookie-directory", SESSION_DIR,
                "--password-provider", "console"]
        if self.adp_var.get():
            # icloudpd 1.33+: asks the Apple device to approve web access for ADP accounts
            args.append("--adp-web-access")
        return args

    def _download_args(self, dest):
        """Flags shared by Start and Preview (folder structure + filters)."""
        args = ["--directory", dest, "--folder-structure", self._folder_structure_value()]
        if self.skip_videos_var.get():
            args.append("--skip-videos")
        if self.skip_live_var.get():
            args.append("--skip-live-photos")
        if self.exif_var.get():
            args.append("--set-exif-datetime")
        album = self.album_var.get().strip()
        if album:
            args += ["--album", album]
        if self.from_var.get().strip():
            args += ["--skip-created-before", self.from_var.get().strip()]
        if self.to_var.get().strip():
            args += ["--skip-created-after", self.to_var.get().strip()]
        if self.quick_var.get():
            n = self.until_n_var.get().strip()
            if not n.isdigit() or int(n) <= 0:
                messagebox.showerror(APP_TITLE, "Quick update: enter a positive whole number.")
                return None
            args += ["--until-found", n]
        # advanced free-text
        if self.extra_var.get().strip():
            try:
                extra = shlex.split(self.extra_var.get().strip())
            except ValueError:
                messagebox.showerror(APP_TITLE, "Could not parse the advanced options.")
                return None
            for tok in extra:
                if tok in FORBIDDEN_FLAGS:
                    messagebox.showerror(
                        APP_TITLE,
                        "For safety this app refuses '%s'. It only ever downloads; "
                        "it never deletes from iCloud." % tok)
                    return None
            args += extra
        return args

    def _folder_structure_value(self):
        label = self.folder_var.get()
        for lbl, val in self.FOLDER_CHOICES:
            if lbl == label:
                return val
        return "{:%Y/%m}"

    def _icloudpd_or_warn(self):
        exe = find_icloudpd()
        if not exe:
            messagebox.showerror(
                APP_TITLE,
                "Could not find 'icloudpd'.\n\nPlease run install.sh first, or launch "
                "this app via the provided launcher so it uses the right Python.")
        return exe

    # -- actions ------------------------------------------------------------ #
    def _on_test(self):
        v = self._validate(need_dir=False)
        if not v:
            return
        exe = self._icloudpd_or_warn()
        if not exe:
            return
        email, pw, _dest = v
        os.makedirs(SESSION_DIR, exist_ok=True)
        argv = [exe] + self._common_args(email) + ["--auth-only"]
        self._launch(argv, pw, mode="test", dest=None)

    def _on_list_albums(self):
        v = self._validate(need_dir=False)
        if not v:
            return
        exe = self._icloudpd_or_warn()
        if not exe:
            return
        email, pw, _dest = v
        os.makedirs(SESSION_DIR, exist_ok=True)
        argv = [exe] + self._common_args(email) + ["--list-albums"]
        self._launch(argv, pw, mode="albums", dest=None)

    def _on_preview(self):
        v = self._validate(need_dir=True)
        if not v:
            return
        exe = self._icloudpd_or_warn()
        if not exe:
            return
        email, pw, dest = v
        dargs = self._download_args(dest)
        if dargs is None:
            return
        os.makedirs(SESSION_DIR, exist_ok=True)
        argv = [exe] + self._common_args(email) + dargs + ["--dry-run"]
        self._launch(argv, pw, mode="preview", dest=dest)

    def _on_start(self):
        v = self._validate(need_dir=True)
        if not v:
            return
        exe = self._icloudpd_or_warn()
        if not exe:
            return
        email, pw, dest = v
        dargs = self._download_args(dest)
        if dargs is None:
            return
        # disk-space heads-up
        try:
            free = shutil.disk_usage(dest).free
            if free < 5 * 1024**3:
                if not messagebox.askokcancel(
                        APP_TITLE,
                        "Only %s free on this drive. Large libraries can be hundreds of GB.\n\n"
                        "Continue anyway?" % human_bytes(free)):
                    return
        except Exception:
            pass
        os.makedirs(SESSION_DIR, exist_ok=True)
        argv = [exe] + self._common_args(email) + dargs
        self._launch(argv, pw, mode="download", dest=dest)

    def _on_signout(self):
        if self.runner or self._retry_job is not None:
            messagebox.showinfo(APP_TITLE, "Stop the current task before signing out.")
            return
        if not messagebox.askokcancel(
                APP_TITLE,
                "Sign out and clear this customer's details?\n\n"
                "This clears the saved iCloud session and the Apple ID, password, "
                "destination folder, album and dates, and turns Quick update off -- "
                "ready for the next customer.\n\n"
                "Your downloaded photos are not touched."):
            return
        try:
            if os.path.isdir(SESSION_DIR):
                shutil.rmtree(SESSION_DIR)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "Could not clear the session: %s" % exc)
            return
        removed = remove_keychain_passwords()
        if removed:
            self._log_line("Removed %d iCloud password(s) saved in the Keychain by "
                           "earlier icloudpd versions." % removed)
        # Customer-specific fields. General preferences (folder layout, options) stay.
        for var in (self.email_var, self.pw_var, self.dir_var, self.album_var,
                    self.from_var, self.to_var, self.extra_var):
            var.set("")
        self.quick_var.set(False)
        self._update_free_space()
        self._save_settings()
        self.status_var.set("Signed out -- ready for the next customer")
        self._log_line("Saved session and customer details cleared.")

    def _on_stop(self):
        if self._retry_job is not None:          # waiting to retry -> cancel it
            self._cancel_retry()
            self._close_logfile(None)
            self._set_running(False)
            self.status_var.set("Stopped (press Start to resume where it left off)")
            self._log_line("Auto-retry cancelled.")
            return
        if self.runner:
            self.status_var.set("Stopping...")
            self.runner.stop()

    # -- auto-retry --------------------------------------------------------- #
    def _cancel_retry(self):
        if self._retry_job is not None:
            try:
                self.root.after_cancel(self._retry_job)
            except Exception:
                pass
        self._retry_job = None
        self._retry_at = None

    def _schedule_retry(self, code):
        self._retry_attempt += 1
        delay = self.RETRY_DELAYS[min(self._retry_attempt - 1, len(self.RETRY_DELAYS) - 1)]
        self._retry_at = time.time() + delay
        self._log_line("icloudpd stopped (exit code %d). Auto-retry %d/%d in %ds -- "
                       "already-downloaded files will be skipped."
                       % (code, self._retry_attempt, self.RETRY_MAX, delay))
        self._set_running(True)                  # keep Stop enabled to cancel
        self._tick_retry()

    def _tick_retry(self):
        if self._retry_at is None:
            return
        left = int(round(self._retry_at - time.time()))
        if left <= 0:
            self._retry_job = None
            self._retry_at = None
            argv, pw, mode, dest = self._job
            self._launch(argv, pw, mode, dest, retry=True)
            return
        self.status_var.set("Connection problem -- retrying in %ds (attempt %d/%d). "
                            "Press Stop to cancel." % (left, self._retry_attempt, self.RETRY_MAX))
        self._retry_job = self.root.after(1000, self._tick_retry)

    def _close_logfile(self, code):
        if self.logfile:
            try:
                if code is None:
                    self.logfile.write("\nStopped. Downloaded: %d\n" % self.downloaded)
                else:
                    self.logfile.write("\nFinished (exit code %d). Downloaded: %d, "
                                       "already present: %d\n"
                                       % (code, self.downloaded, self.skipped))
                self.logfile.close()
            except Exception:
                pass
            if self.logfile_path:
                self._log_line("Log saved: %s" % self.logfile_path)
            self.logfile = None

    def _launch(self, argv, pw, mode, dest, retry=False):
        self.mode = mode
        self.dest = dest
        self._job = (argv, pw, mode, dest)
        self.total = 0
        self.cur = 0
        if not retry:
            self.downloaded = 0                  # new files: cumulative across retries
            self._retry_attempt = 0
        self._downloaded_at_launch = self.downloaded
        self.skipped = 0
        if not retry or not self.start_time:
            self.start_time = time.time()
        self._errors_shown = set()
        self._last_error_line = None
        self._last_rate = None
        self._last_eta = None
        self.pbar.configure(mode="determinate", value=0)
        self._update_stats()
        self.meta_var.set("Speed: -   ETA: -   Elapsed: 0s")
        self.current_var.set("")
        labels = {"download": "Starting...", "preview": "Previewing (no files saved)...",
                  "test": "Signing in...", "albums": "Listing albums..."}
        self.status_var.set(labels.get(mode, "Starting..."))

        # per-job log file (real downloads only; a retry keeps writing to the same one)
        if not retry:
            self.logfile = None
            self.logfile_path = None
        if not retry and mode == "download" and self.save_log_var.get() and dest:
            try:
                stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
                self.logfile_path = os.path.join(dest, "icloud_download_log_%s.txt" % stamp)
                self.logfile = open(self.logfile_path, "w")
                self.logfile.write("iCloud Photo Downloader log -- %s\n" % stamp)
            except Exception:
                self.logfile = None

        self._save_settings()
        self._log_line("$ " + " ".join(shlex.quote(a) for a in argv))
        self._set_running(True)
        self.runner = IcloudpdRunner(
            argv, pw, self.q, self._ask_mfa_blocking,
            keep_awake=self.keep_awake_var.get() and mode in ("download", "preview"))
        self.runner.start()

    def _set_running(self, running):
        state = "disabled" if running else "normal"
        for b in (self.test_btn, self.preview_btn, self.start_btn, self.signout_btn):
            b.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")

    # -- 2FA dialog --------------------------------------------------------- #
    def _ask_mfa_blocking(self, sms=False, devices=None):
        self._mfa_result = None
        self._mfa_event = threading.Event()
        self.root.after(0, lambda: self._show_mfa_dialog(sms, devices or []))
        self._mfa_event.wait()
        return self._mfa_result

    def _show_mfa_dialog(self, sms, devices):
        if sms:
            text = "Enter the 6-digit code Apple sent by SMS:"
        else:
            text = ("Apple sent a 6-digit verification code to your trusted device(s).\n"
                    "Enter it here:")
            if devices:
                text += ("\n\nNo code on your devices? Instead type the letter of a "
                         "phone number to get the code by SMS:\n" +
                         "\n".join("   %s:  %s" % d for d in devices))
        code = simpledialog.askstring(APP_TITLE, text, parent=self.root)
        self._mfa_result = (code or "").strip().replace(" ", "")
        if self._mfa_event:
            self._mfa_event.set()

    # -- queue polling ------------------------------------------------------ #
    def _poll(self):
        try:
            while True:
                self._apply(self.q.get_nowait())
        except queue.Empty:
            pass
        if self.runner and self.start_time:
            self._refresh_meta()
        self.root.after(self.POLL_MS, self._poll)

    def _apply(self, ev):
        t = ev.get("type")
        if t == "status":
            self.status_var.set(ev["value"])
        elif t == "total":
            self.total = ev["value"]
            self._update_stats()
        elif t == "progress":
            if ev.get("total"):
                self.total = ev["total"]
            if ev.get("cur") is not None:
                self.cur = ev["cur"]
            pct = ev.get("pct")
            if pct is None and self.total:
                pct = min(100, int(self.cur * 100 / self.total))
            if pct is not None:
                self.pbar.configure(value=min(100, pct))
            self._last_rate = ev.get("rate")
            self._last_eta = ev.get("eta")
            self._update_stats()
        elif t == "downloaded":
            self.downloaded += 1
            if ev.get("file"):
                self.current_var.set("Last: " + os.path.basename(ev["file"]))
            self._update_stats()
        elif t == "skipped":
            self.skipped += 1
            self._update_stats()
        elif t == "current":
            if ev.get("file"):
                self.current_var.set("Now: " + os.path.basename(ev["file"]))
        elif t == "log":
            self._log_line(ev["line"])
        elif t == "error":
            self._log_line("ERROR: " + ev["msg"])
            if ev["msg"] not in self._errors_shown:   # one popup per distinct problem
                self._errors_shown.add(ev["msg"])
                messagebox.showerror(APP_TITLE, ev["msg"])
        elif t == "error_line":
            self._last_error_line = ev["line"]
        elif t == "info":
            if ev["msg"] not in self._errors_shown:
                self._errors_shown.add(ev["msg"])
                messagebox.showinfo(APP_TITLE, ev["msg"])
        elif t == "finished":
            self._on_finished(ev.get("code", 0))

    def _processed(self):
        return max(self.cur, self.downloaded + self.skipped)

    def _update_stats(self):
        processed = self._processed()
        remaining = (self.total - processed) if self.total else None
        if remaining is not None and remaining < 0:
            remaining = 0
        self.stats_var.set(
            "Total: %s   Downloaded: %d   Already on disk: %d   Remaining: %s" % (
                self.total if self.total else "-", self.downloaded, self.skipped,
                remaining if remaining is not None else "-"))
        if self.total and not getattr(self, "_last_rate", None):
            self.pbar.configure(value=min(100, int(processed * 100 / self.total)))

    def _refresh_meta(self):
        elapsed = int(time.time() - self.start_time)
        rate = getattr(self, "_last_rate", None)
        eta = getattr(self, "_last_eta", None)
        self.meta_var.set("Speed: %s   ETA: %s   Elapsed: %s" % (
            ("%.1f items/s" % rate) if rate else "-",
            eta if eta else "-", self._fmt_elapsed(elapsed)))

    @staticmethod
    def _fmt_elapsed(secs):
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return "%dh %02dm %02ds" % (h, m, s)
        if m:
            return "%dm %02ds" % (m, s)
        return "%ds" % s

    def _notify_done(self, msg):
        try:
            self.root.bell()
        except Exception:
            pass
        try:
            subprocess.Popen(
                ["osascript", "-e",
                 'display notification %s with title %s' % (json.dumps(msg), json.dumps(APP_TITLE))],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _on_finished(self, code):
        self._set_running(False)
        stopped = self.runner._stop_requested if self.runner else False
        self.runner = None
        mode = self.mode
        self._refresh_meta()
        self._update_free_space()

        # Auto-retry: only real downloads, only unexpected failures. Known problems
        # (wrong password, locked account, ACCESS_DENIED, ...) were explained in a
        # popup and are not retried -- retrying a sign-in can make Apple lock the account.
        if (mode == "download" and code != 0 and not stopped
                and self.retry_var.get() and not self._errors_shown):
            if self.downloaded > self._downloaded_at_launch:
                self._retry_attempt = 0          # it made progress: fresh retry budget
            if self._retry_attempt < self.RETRY_MAX:
                self._schedule_retry(code)
                return
            self._log_line("Auto-retry gave up after %d attempts without progress."
                           % self.RETRY_MAX)

        self._close_logfile(code)

        if stopped:
            self.status_var.set("Stopped (press Start to resume where it left off)")
            return
        if code != 0:
            self.status_var.set("Finished with errors (exit code %d)" % code)
            # icloudpd 1.33+ reports failures with a real exit code; if no known
            # error was explained already, show the last ERROR line it printed.
            if not self._errors_shown:
                detail = self._last_error_line or "See the activity log for details."
                messagebox.showerror(APP_TITLE, "icloudpd stopped with an error:\n\n%s" % detail)
            return

        if mode == "preview":
            self.status_var.set("Preview done")
            free = ""
            try:
                if self.dest:
                    free = "\nFree space on drive: %s" % human_bytes(shutil.disk_usage(self.dest).free)
            except Exception:
                pass
            messagebox.showinfo(
                APP_TITLE,
                "Items matching your settings: %s%s\n\n(No files were downloaded.)"
                % (self.total if self.total else "unknown", free))
        elif mode == "test":
            self.status_var.set("Signed in -- session saved")
            messagebox.showinfo(APP_TITLE, "Sign-in OK. The session is saved.")
        elif mode == "albums":
            self.status_var.set("Albums listed (see the activity log)")
        else:  # download
            self.pbar.configure(value=100)
            self.status_var.set("Completed")
            if self.sound_var.get():
                self._notify_done("Download finished: %d new, %d already present."
                                  % (self.downloaded, self.skipped))
            messagebox.showinfo(
                APP_TITLE,
                "Done.\n\nDownloaded this run: %d\nAlready present: %d"
                % (self.downloaded, self.skipped))

    def _on_close(self):
        self._save_settings()
        if self._retry_job is not None:
            self._cancel_retry()
            self._close_logfile(None)
        if self.runner:
            if not messagebox.askokcancel(
                    APP_TITLE,
                    "A task is running. Stop it and quit?\n\n"
                    "(You can resume later -- already-downloaded files are kept.)"):
                return
            self.runner.stop()
            for _ in range(20):
                if not (self.runner and self.runner.is_alive()):
                    break
                time.sleep(0.1)
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
