import ctypes
import json
import os
import subprocess
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# HEADROOM_DATA lets a test copy run beside a real install without sharing state
DATA_DIR = os.environ.get("HEADROOM_DATA") or os.path.join(os.environ.get("APPDATA", APP_DIR), "Headroom")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "headroom.log")
SCAN_FILE = os.path.join(DATA_DIR, "scan.json")
PROFILE_FILE = os.path.join(DATA_DIR, "profile.json")       # the active profile for THIS PC
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
BUILTIN_DIR = os.path.join(APP_DIR, "profiles", "builtin")    # researched profiles shipped with the app
FAMILIES_FILE = os.path.join(APP_DIR, "profiles", "families.json")
# tools (CoreCycler, LibreHardwareMonitor) are downloaded per user, never shipped
TOOLS_DIR = os.path.join(os.environ.get("LOCALAPPDATA", DATA_DIR), "Headroom", "tools")
VENDOR_DIR = os.path.join(APP_DIR, "vendor")                   # pip packages (anthropic SDK) in dev checkouts
if os.path.isdir(VENDOR_DIR) and VENDOR_DIR not in sys.path:
    sys.path.append(VENDOR_DIR)

NO_WINDOW = 0x08000000


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


SYS32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
_SYS_TOOLS = {"powershell.exe": os.path.join(SYS32, "WindowsPowerShell", "v1.0", "powershell.exe")}


def _resolve(exe):
    """Absolute path for Windows tools: PATH can be incomplete when started
    from a scheduled task or another app."""
    if os.path.dirname(exe):
        return exe
    if exe in _SYS_TOOLS:
        return _SYS_TOOLS[exe]
    for name in (exe, exe + ".exe"):
        p = os.path.join(SYS32, name)
        if os.path.exists(p):
            return p
    return exe


def run(cmd, timeout=30):
    """Run a command hidden, return stdout text ('' on failure)."""
    cmd = [_resolve(cmd[0])] + list(cmd[1:])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        return r.stdout
    except Exception:  # noqa: BLE001
        return ""


def ps(script, timeout=30):
    return run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout)


def process_running(*names):
    out = run(["tasklist", "/fo", "csv", "/nh"]).lower()
    return [n for n in names if ('"%s"' % n.lower()) in out]


class State:
    """Small JSON store. Written on every change, so a crash or freeze in the
    middle of a test leaves an accurate record of what was being tested."""

    def __init__(self, path=STATE_FILE):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self.d = json.load(f)
        except Exception:  # noqa: BLE001
            self.d = {}

    def get(self, *keys, default=None):
        d = self.d
        for k in keys:
            if not isinstance(d, dict) or k not in d:
                return default
            d = d[k]
        return d

    def set(self, *keys, value):
        with self.lock:
            d = self.d
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = value
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.d, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)


class Log:
    def __init__(self, path=LOG_FILE):
        self.path = path
        self.listeners = []

    def __call__(self, msg, level="info"):
        line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        for fn in list(self.listeners):
            try:
                fn(line, level)
            except Exception:  # noqa: BLE001
                pass


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_profiles():
    """The active profile for this PC (written by setup). Empty until setup has run."""
    return read_json(PROFILE_FILE, {}) or {}


def match_profile(section, name, profiles=None):
    p = (profiles or load_profiles()).get(section, {})
    name = (name or "").lower()
    for k, v in p.items():
        if k.lower() in name or name in k.lower():
            return k, v
    return None, None


def tdr_events_since(t0):
    """NVIDIA driver resets / Xid errors logged in the System event log since
    t0 (epoch seconds). Any of these during a test = unstable."""
    ms = max(1000, int((time.time() - t0 + 5) * 1000))
    q = ("*[System[(Provider[@Name='nvlddmkm'] or (Provider[@Name='Display'] and EventID=4101)) "
         "and TimeCreated[timediff(@SystemTime) <= %d]]]" % ms)
    out = run(["wevtutil", "qe", "System", "/q:" + q, "/f:text", "/c:10", "/rd:true"], 15)
    events = []
    for block in out.split("Event[")[1:]:
        eid = desc = ""
        for ln in block.splitlines():
            ln = ln.strip()
            if ln.startswith("Event ID:"):
                eid = ln.split(":", 1)[1].strip()
            elif ln.startswith("Description:"):
                desc = ln.split(":", 1)[1].strip()
        events.append("event %s %s" % (eid, desc[:120]))
    return events


def whea_events_since(t0):
    ms = max(1000, int((time.time() - t0 + 5) * 1000))
    q = ("*[System[Provider[@Name='Microsoft-Windows-WHEA-Logger'] "
         "and TimeCreated[timediff(@SystemTime) <= %d]]]" % ms)
    out = run(["wevtutil", "qe", "System", "/q:" + q, "/f:text", "/c:20", "/rd:true"], 15)
    return out.count("Event[")
