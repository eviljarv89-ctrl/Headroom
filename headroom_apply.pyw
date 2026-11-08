"""Run at logon by the 'Headroom GPU Apply' task. Applies the saved GPU
profile, but NOT if the last shutdown was a crash / power loss / bugcheck:
then the PC boots at stock and the reason is logged."""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from headroom.common import Log, State, run  # noqa: E402
from headroom.gpu_tune import apply_saved  # noqa: E402
from headroom.nvml import Nvml  # noqa: E402

log = Log()
up_ms = ctypes.windll.kernel32.GetTickCount64()
q = ("*[System[((Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=41) or "
     "(Provider[@Name='Microsoft-Windows-WER-SystemErrorReporting'] and EventID=1001) or "
     "(Provider[@Name='EventLog'] and EventID=6008)) and TimeCreated[timediff(@SystemTime) <= %d]]]" % (up_ms + 120000))
crash = "Event[" in run(["wevtutil", "qe", "System", "/q:" + q, "/c:1", "/f:text"])
if crash:
    log("Logon apply SKIPPED: the last shutdown was unexpected (crash, freeze or power loss). GPU left at stock.", "warn")
else:
    try:
        apply_saved(Nvml(), State(), log)
    except Exception as e:  # noqa: BLE001
        log("Logon apply failed: %s" % e, "error")
