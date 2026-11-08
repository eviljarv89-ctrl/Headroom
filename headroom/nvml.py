"""Minimal NVML binding (ctypes, no dependencies).

Only the calls Headroom needs. Clock offsets use the newer
nvmlDeviceSetClockOffsets API: on this driver (617.x, RTX 50 series) the
older nvmlDeviceSetGpcClkVfOffset returns NOT_SUPPORTED.
"""
import ctypes as C
import os

NVML_SUCCESS = 0
ERRORS = {
    1: "uninitialized", 2: "invalid argument", 3: "not supported",
    4: "no permission (run Headroom as administrator)", 5: "already initialized",
    6: "not found", 8: "insufficient power", 9: "driver not loaded",
    10: "timeout", 12: "lib rm version mismatch", 15: "GPU is lost", 999: "unknown",
}

CLK_GRAPHICS, CLK_SM, CLK_MEM = 0, 1, 2
TEMP_GPU = 0


class NvmlError(RuntimeError):
    def __init__(self, fn, code):
        self.code = code
        super().__init__("%s failed: %s (%d)" % (fn, ERRORS.get(code, "error"), code))


class _ClockOffset(C.Structure):
    _fields_ = [("version", C.c_uint), ("type", C.c_int), ("pstate", C.c_int),
                ("clockOffsetMHz", C.c_int), ("minClockOffsetMHz", C.c_int),
                ("maxClockOffsetMHz", C.c_int)]


class _Util(C.Structure):
    _fields_ = [("gpu", C.c_uint), ("memory", C.c_uint)]


_CLOCKOFFSET_V1 = C.sizeof(_ClockOffset) | (1 << 24)

# Throttle reason bits (nvmlClocksEventReasons)
THROTTLE = {
    0x1: "idle", 0x2: "app clocks", 0x4: "power cap", 0x8: "HW slowdown",
    0x20: "SW thermal", 0x40: "HW thermal", 0x80: "power brake", 0x100: "display clocks",
}


class Nvml:
    def __init__(self, index=0):
        path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "nvml.dll")
        self.lib = C.WinDLL(path)
        self._chk("nvmlInit_v2", self.lib.nvmlInit_v2())
        self.h = C.c_void_p()
        self._chk("nvmlDeviceGetHandleByIndex_v2",
                  self.lib.nvmlDeviceGetHandleByIndex_v2(index, C.byref(self.h)))
        self.api = self._detect_offset_api()

    def _detect_offset_api(self):
        """nvmlDeviceSetClockOffsets (newer drivers) or the older per-domain
        VF-offset calls. None = clock offsets can't be changed on this card/driver."""
        try:
            s = _ClockOffset(version=_CLOCKOFFSET_V1, type=CLK_GRAPHICS, pstate=0)
            if self.lib.nvmlDeviceGetClockOffsets(self.h, C.byref(s)) == NVML_SUCCESS:
                return "clock_offsets"
        except AttributeError:
            pass
        return "vf_offset" if self.vf_offset_supported() else None

    def vf_offset_supported(self):
        v = C.c_int()
        try:
            return self.lib.nvmlDeviceGetGpcClkVfOffset(self.h, C.byref(v)) == NVML_SUCCESS
        except AttributeError:
            return False

    def mem_total_bytes(self):
        class Mem(C.Structure):
            _fields_ = [("total", C.c_ulonglong), ("free", C.c_ulonglong), ("used", C.c_ulonglong)]
        m = Mem()
        self._chk("nvmlDeviceGetMemoryInfo", self.lib.nvmlDeviceGetMemoryInfo(self.h, C.byref(m)))
        return m.total

    def _chk(self, fn, r):
        if r != NVML_SUCCESS:
            raise NvmlError(fn, r)

    def _uint(self, fn, *args):
        v = C.c_uint()
        self._chk(fn, getattr(self.lib, fn)(self.h, *args, C.byref(v)))
        return v.value

    # ---- info -------------------------------------------------------------
    def name(self):
        b = C.create_string_buffer(96)
        self._chk("nvmlDeviceGetName", self.lib.nvmlDeviceGetName(self.h, b, 96))
        return b.value.decode()

    def vbios(self):
        b = C.create_string_buffer(32)
        self._chk("nvmlDeviceGetVbiosVersion", self.lib.nvmlDeviceGetVbiosVersion(self.h, b, 32))
        return b.value.decode()

    def driver(self):
        b = C.create_string_buffer(80)
        self._chk("nvmlSystemGetDriverVersion", self.lib.nvmlSystemGetDriverVersion(b, 80))
        return b.value.decode()

    def pci_subsys(self):
        class Pci(C.Structure):
            _fields_ = [("busIdLegacy", C.c_char * 16), ("domain", C.c_uint), ("bus", C.c_uint),
                        ("device", C.c_uint), ("pciDeviceId", C.c_uint),
                        ("pciSubSystemId", C.c_uint), ("reserved", C.c_uint * 4),
                        ("busId", C.c_char * 32)]
        p = Pci()
        self._chk("nvmlDeviceGetPciInfo_v3", self.lib.nvmlDeviceGetPciInfo_v3(self.h, C.byref(p)))
        return p.pciDeviceId, p.pciSubSystemId

    def clock(self, kind):
        return self._uint("nvmlDeviceGetClockInfo", kind)

    def max_clock(self, kind):
        return self._uint("nvmlDeviceGetMaxClockInfo", kind)

    def temp(self):
        return self._uint("nvmlDeviceGetTemperature", TEMP_GPU)

    def power_w(self):
        return self._uint("nvmlDeviceGetPowerUsage") / 1000.0

    def power_limit_w(self):
        return self._uint("nvmlDeviceGetPowerManagementLimit") / 1000.0

    def power_limits_w(self):
        lo, hi = C.c_uint(), C.c_uint()
        self._chk("nvmlDeviceGetPowerManagementLimitConstraints",
                  self.lib.nvmlDeviceGetPowerManagementLimitConstraints(self.h, C.byref(lo), C.byref(hi)))
        return lo.value / 1000.0, hi.value / 1000.0

    def util(self):
        u = _Util()
        self._chk("nvmlDeviceGetUtilizationRates", self.lib.nvmlDeviceGetUtilizationRates(self.h, C.byref(u)))
        return u.gpu, u.memory

    def fan_pct(self):
        try:
            return self._uint("nvmlDeviceGetFanSpeed")
        except NvmlError:
            return None

    def throttle(self):
        v = C.c_ulonglong()
        r = self.lib.nvmlDeviceGetCurrentClocksEventReasons(self.h, C.byref(v))
        if r != 0:
            return []
        return [n for bit, n in THROTTLE.items() if v.value & bit]

    def pstate(self):
        v = C.c_int()
        self._chk("nvmlDeviceGetPerformanceState", self.lib.nvmlDeviceGetPerformanceState(self.h, C.byref(v)))
        return v.value

    # ---- fans (set needs admin) -------------------------------------------
    def num_fans(self):
        try:
            return self._uint("nvmlDeviceGetNumFans")
        except NvmlError:
            return 0

    def fan_limits(self):
        lo, hi = C.c_uint(), C.c_uint()
        r = self.lib.nvmlDeviceGetMinMaxFanSpeed(self.h, C.byref(lo), C.byref(hi))
        return (lo.value, hi.value) if r == 0 else (30, 100)

    def set_fans(self, pct):
        for i in range(self.num_fans()):
            self._chk("nvmlDeviceSetFanSpeed_v2", self.lib.nvmlDeviceSetFanSpeed_v2(self.h, i, int(pct)))

    def reset_fans(self):
        """Hand the fans back to the card's own curve (incl. zero-rpm at idle)."""
        for i in range(self.num_fans()):
            self._chk("nvmlDeviceSetDefaultFanSpeed_v2", self.lib.nvmlDeviceSetDefaultFanSpeed_v2(self.h, i))

    # ---- offsets ----------------------------------------------------------
    def get_offset(self, kind, pstate=0):
        if self.api == "vf_offset":
            v, lo, hi = C.c_int(), C.c_int(), C.c_int()
            pre = "nvmlDeviceGetGpcClk" if kind == CLK_GRAPHICS else "nvmlDeviceGetMemClk"
            self._chk(pre + "VfOffset", getattr(self.lib, pre + "VfOffset")(self.h, C.byref(v)))
            try:
                self._chk(pre + "MinMaxVfOffset", getattr(self.lib, pre + "MinMaxVfOffset")(self.h, C.byref(lo), C.byref(hi)))
            except (AttributeError, NvmlError):
                lo.value, hi.value = -1000, 1000
            return v.value, lo.value, hi.value
        s = _ClockOffset(version=_CLOCKOFFSET_V1, type=kind, pstate=pstate)
        self._chk("nvmlDeviceGetClockOffsets", self.lib.nvmlDeviceGetClockOffsets(self.h, C.byref(s)))
        return s.clockOffsetMHz, s.minClockOffsetMHz, s.maxClockOffsetMHz

    def set_offset(self, kind, mhz, pstates=(0, 2)):
        """Apply to P0 (games) and P2 (CUDA/compute) so both see the same clocks."""
        if self.api == "vf_offset":
            fn = "nvmlDeviceSetGpcClkVfOffset" if kind == CLK_GRAPHICS else "nvmlDeviceSetMemClkVfOffset"
            self._chk(fn, getattr(self.lib, fn)(self.h, C.c_int(int(mhz))))
            return
        if self.api is None:
            raise NvmlError("clock offsets (not supported by this card or driver)", 3)
        for ps in pstates:
            s = _ClockOffset(version=_CLOCKOFFSET_V1, type=kind, pstate=ps, clockOffsetMHz=int(mhz))
            self._chk("nvmlDeviceSetClockOffsets", self.lib.nvmlDeviceSetClockOffsets(self.h, C.byref(s)))

    def lock_gpu_clocks(self, lo, hi):
        self._chk("nvmlDeviceSetGpuLockedClocks", self.lib.nvmlDeviceSetGpuLockedClocks(self.h, int(lo), int(hi)))

    def reset_gpu_clocks(self):
        self._chk("nvmlDeviceResetGpuLockedClocks", self.lib.nvmlDeviceResetGpuLockedClocks(self.h))

    def set_power_limit_w(self, w):
        self._chk("nvmlDeviceSetPowerManagementLimit",
                  self.lib.nvmlDeviceSetPowerManagementLimit(self.h, int(w * 1000)))

    def reset_all(self):
        """Back to stock: offsets 0, clock lock removed, default power limit."""
        errs = []
        for kind in (CLK_GRAPHICS, CLK_MEM):
            try:
                self.set_offset(kind, 0)
            except NvmlError as e:
                errs.append(str(e))
        try:
            self.reset_gpu_clocks()
        except NvmlError as e:
            if e.code != 3:
                errs.append(str(e))
        try:
            d = self._uint("nvmlDeviceGetPowerManagementDefaultLimit") / 1000.0
            if abs(self.power_limit_w() - d) > 0.5:
                self.set_power_limit_w(d)
        except NvmlError as e:
            errs.append(str(e))
        return errs
