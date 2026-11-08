"""Sensor access: LibreHardwareMonitor web server (CPU/board/GPU memory junction)
plus NVML for the GPU. LHM must be running with its web server on :8085
(it already autostarts elevated on this PC)."""
import json
import re
import threading
import time
import urllib.request

LHM_URL = "http://127.0.0.1:8085/data.json"


def _num(v):
    if v is None:
        return None
    m = re.match(r"\s*(-?[\d.,]+)", str(v))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


class Lhm:
    """Flattens data.json into {SensorId: (type, text, value, hardware)}."""

    def __init__(self, url=LHM_URL):
        self.url = url
        self.ok = False
        self.err = ""
        self.s = {}

    def poll(self):
        try:
            with urllib.request.urlopen(self.url, timeout=2) as r:
                d = json.loads(r.read().decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            self.ok, self.err = False, str(e)
            return False
        out = {}

        def walk(n, hw):
            if n.get("SensorId") and n.get("Type"):
                out[n["SensorId"]] = (n["Type"], n.get("Text", ""), _num(n.get("Value")), hw)
            for c in n.get("Children", []):
                walk(c, hw if n.get("SensorId") else n.get("Text", hw))
        walk(d, "")
        self.s, self.ok, self.err = out, True, ""
        return True

    def val(self, sid):
        v = self.s.get(sid)
        return v[2] if v else None

    def find(self, prefix, typ, text_re):
        rx = re.compile(text_re)
        for sid, (t, txt, v, _) in self.s.items():
            if sid.startswith(prefix) and t == typ and rx.search(txt):
                return v
        return None

    def all(self, prefix, typ, text_re):
        rx = re.compile(text_re)
        return [(txt, v) for sid, (t, txt, v, _) in sorted(self.s.items())
                if sid.startswith(prefix) and t == typ and rx.search(txt)]

    # ---- convenience views --------------------------------------------------
    def _cpu_prefix(self):
        for p in ("/amdcpu/0/", "/intelcpu/0/"):
            if any(k.startswith(p) for k in self.s):
                return p
        return "/amdcpu/0/"

    def cpu(self):
        p = self._cpu_prefix()
        eff = self.all(p, "Clock", r"^Core #\d+ \(Effective\)")
        tdie = self.find(p, "Temperature", r"CCD1 \(Tdie\)") or self.find(p, "Temperature", r"^Core \(Tctl/Tdie\)$")
        pkg_t = self.find(p, "Temperature", r"^CPU Package$") or self.find(p, "Temperature", r"^Core Max$")
        return {
            "tdie": tdie if tdie is not None else pkg_t,   # the temperature the safety limits use
            "tctl": self.find(p, "Temperature", r"Tctl"),
            "package_w": self.find(p, "Power", r"^(Package|CPU Package)$"),
            "eff_avg": self.find(p, "Clock", r"Average Effective"),
            "eff_cores": [v for _, v in eff],
            "max_core_clock": max([v for _, v in self.all(p, "Clock", r"^(Core|P-Core) #\d+$") if v] or [0]),
            "load": self.find(p, "Load", r"CPU Total"),
            "vcore": self.find("/lpc/", "Voltage", r"^Vcore$") or self.find(p, "Voltage", r"^Core( \(SVI\d TFN\))?$"),
        }

    def gpu_extra(self):
        p = "/gpu-nvidia/0/"
        return {
            "mem_junction": self.find(p, "Temperature", r"Memory Junction"),
            "hotspot": self.find(p, "Temperature", r"Hot ?Spot"),
            "vcore": self.find(p, "Voltage", r"Core Voltage"),
            "fans": [v for _, v in self.all(p, "Fan", r"GPU Fan")],
        }

    def board_fans(self):
        return {txt: v for txt, v in self.all("/lpc/", "Fan", r"Fan")}


class Telemetry(threading.Thread):
    """Background sampler shared by the GUI and the tuners. Readers get the
    latest snapshot via .snap; nothing here ever changes hardware."""

    def __init__(self, nvml=None, period=1.0):
        super().__init__(daemon=True)
        self.nv = nvml
        self.lhm = Lhm()
        self.period = period
        self.snap = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.period)

    def sample(self):
        s = {"t": time.time()}
        if self.nv:
            try:
                s["gpu"] = {
                    "temp": self.nv.temp(), "power": self.nv.power_w(),
                    "clk": self.nv.clock(0), "mem_clk": self.nv.clock(2),
                    "util": self.nv.util()[0], "fan": self.nv.fan_pct(),
                    "throttle": self.nv.throttle(), "pstate": self.nv.pstate(),
                }
            except Exception as e:  # noqa: BLE001
                s["gpu"] = {"error": str(e)}
        if self.lhm.poll():
            s["cpu"] = self.lhm.cpu()
            s["gpu_x"] = self.lhm.gpu_extra()
            s["fans"] = self.lhm.board_fans()
        else:
            s["lhm_error"] = self.lhm.err
        with self.lock:
            self.snap = s
        return s

    def get(self):
        with self.lock:
            return dict(self.snap)

    def stop(self):
        self._stop.set()
