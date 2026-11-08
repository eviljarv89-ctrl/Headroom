"""GPU stress + error detection through the CUDA driver API (nvcuda.dll ships
with the NVIDIA driver, so no toolkit, torch or cupy needed). Kernels are
hand-written PTX that the driver JIT-compiles.

core test   : 8 chaotic logistic-map chains per thread in FP32 FMA. The map
              is chaotic, so a single flipped bit anywhere grows into a
              completely different result: any computation error from an
              unstable core clock shows up as a checksum mismatch against the
              golden result captured at stock clocks.
memory test : fills VRAM with a hash pattern (128-bit stores) and reads it
              back, counting mismatches. Also measures bandwidth: GDDR7
              retries transfers on errors (EDC), so an unstable memory clock
              usually shows up first as LOST bandwidth, before real errors.
"""
import ctypes as C
import hashlib
import os
import struct
import time

HASH = """
    xor.b32 {o}, {i}, {s};
    shr.u32 {t}, {o}, 16;  xor.b32 {o}, {o}, {t};
    mul.lo.u32 {o}, {o}, 0x7feb352d;
    shr.u32 {t}, {o}, 15;  xor.b32 {o}, {o}, {t};
    mul.lo.u32 {o}, {o}, 0x846ca68b;
    shr.u32 {t}, {o}, 16;  xor.b32 {o}, {o}, {t};
"""


def _ptx():
    chains = 8
    init = []
    step = []
    for k in range(chains):
        init.append("    fma.rn.f32 %%f%d, %%f0, 0f3F4CCCCD, 0f%08X;" % (
            k + 1, struct.unpack("<I", struct.pack("<f", 0.05 + 0.005 * k))[0]))
        step.append("    neg.f32 %%g%d, %%f%d;\n    fma.rn.f32 %%g%d, %%g%d, %%f%d, %%f%d;\n"
                    "    mul.rn.f32 %%f%d, %%g%d, %%f20;" % (k, k + 1, k, k, k + 1, k + 1, k + 1, k))
    sums = "\n".join("    add.f32 %%f9, %%f9, %%f%d;" % (k + 1) for k in range(1, chains))

    fill_lanes, check_lanes = [], []
    for k in range(4):
        fill_lanes.append("    add.u32 %%r10, %%r9, %d;" % k + HASH.format(o="%%rv%d" % k, i="%r10", s="%r3", t="%r11"))
        check_lanes.append("    add.u32 %%r10, %%r9, %d;" % k + HASH.format(o="%%rh%d" % k, i="%r10", s="%r3", t="%r11")
                           + "    setp.ne.u32 %%q, %%rv%d, %%rh%d;\n    @%%q add.u32 %%r12, %%r12, 1;" % (k, k))
    fill_lanes = "\n".join(fill_lanes).replace("%%", "%")
    check_lanes = "\n".join(check_lanes).replace("%%", "%")

    return r"""
.version 6.0
.target sm_50
.address_size 64

.visible .entry burn(.param .u64 p_out, .param .u32 p_iters, .param .u32 p_seed)
{
    .reg .pred %p;
    .reg .b32 %r<12>;
    .reg .f32 %f<24>;
    .reg .f32 %g<8>;
    .reg .b64 %rd<4>;
    ld.param.u64 %rd1, [p_out];
    ld.param.u32 %r1, [p_iters];
    ld.param.u32 %r2, [p_seed];
    mov.u32 %r3, %ctaid.x;
    mov.u32 %r4, %ntid.x;
    mov.u32 %r5, %tid.x;
    mad.lo.u32 %r6, %r3, %r4, %r5;
    xor.b32 %r7, %r6, %r2;
    mul.lo.u32 %r7, %r7, 0x9E3779B1;
    shr.u32 %r8, %r7, 9;
    or.b32 %r8, %r8, 0x3F800000;
    mov.b32 %f0, %r8;
    sub.f32 %f0, %f0, 0f3F800000;
""" + "\n".join(init) + r"""
    mov.f32 %f20, 0f407F5C29;
LOOP:
""" + "\n".join(step) + r"""
    sub.u32 %r1, %r1, 1;
    setp.ne.u32 %p, %r1, 0;
    @%p bra LOOP;
    mov.f32 %f9, %f1;
""" + sums + r"""
    mul.wide.u32 %rd2, %r6, 4;
    add.u64 %rd3, %rd1, %rd2;
    st.global.f32 [%rd3], %f9;
    ret;
}

.visible .entry mfill(.param .u64 p_buf, .param .u32 p_n4, .param .u32 p_seed)
{
    .reg .pred %p;
    .reg .b32 %r<14>;
    .reg .b32 %rv<4>;
    .reg .b64 %rd<4>;
    ld.param.u64 %rd1, [p_buf];
    ld.param.u32 %r2, [p_n4];
    ld.param.u32 %r3, [p_seed];
    mov.u32 %r4, %ctaid.x;
    mov.u32 %r5, %ntid.x;
    mov.u32 %r6, %tid.x;
    mad.lo.u32 %r7, %r4, %r5, %r6;
    mov.u32 %r8, %nctaid.x;
    mul.lo.u32 %r8, %r8, %r5;
FL:
    setp.ge.u32 %p, %r7, %r2;
    @%p bra FE;
    shl.b32 %r9, %r7, 2;
""" + fill_lanes + r"""
    mul.wide.u32 %rd2, %r7, 16;
    add.u64 %rd3, %rd1, %rd2;
    st.global.v4.u32 [%rd3], {%rv0, %rv1, %rv2, %rv3};
    add.u32 %r7, %r7, %r8;
    bra FL;
FE:
    ret;
}

.visible .entry mcheck(.param .u64 p_buf, .param .u32 p_n4, .param .u32 p_seed, .param .u64 p_err)
{
    .reg .pred %p;
    .reg .pred %q;
    .reg .b32 %r<14>;
    .reg .b32 %rv<4>;
    .reg .b32 %rh<4>;
    .reg .b64 %rd<6>;
    ld.param.u64 %rd1, [p_buf];
    ld.param.u32 %r2, [p_n4];
    ld.param.u32 %r3, [p_seed];
    ld.param.u64 %rd4, [p_err];
    mov.u32 %r4, %ctaid.x;
    mov.u32 %r5, %ntid.x;
    mov.u32 %r6, %tid.x;
    mad.lo.u32 %r7, %r4, %r5, %r6;
    mov.u32 %r8, %nctaid.x;
    mul.lo.u32 %r8, %r8, %r5;
    mov.u32 %r12, 0;
CL:
    setp.ge.u32 %p, %r7, %r2;
    @%p bra CE;
    mul.wide.u32 %rd2, %r7, 16;
    add.u64 %rd3, %rd1, %rd2;
    ld.global.v4.u32 {%rv0, %rv1, %rv2, %rv3}, [%rd3];
    shl.b32 %r9, %r7, 2;
""" + check_lanes + r"""
    add.u32 %r7, %r7, %r8;
    bra CL;
CE:
    setp.eq.u32 %p, %r12, 0;
    @%p bra CX;
    atom.global.add.u32 %r13, [%rd4], %r12;
CX:
    ret;
}
"""


class CudaError(RuntimeError):
    def __init__(self, fn, code):
        self.code = code
        super().__init__("%s -> CUDA error %d" % (fn, code))


class Cuda:
    def __init__(self):
        self.lib = C.WinDLL(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "nvcuda.dll"))
        self.ctx = None
        self.bufs = []
        self._open()

    def _c(self, fn, *a):
        r = getattr(self.lib, fn)(*a)
        if r != 0:
            raise CudaError(fn, r)

    def _open(self):
        self._c("cuInit", 0)
        self.dev = C.c_int()
        self._c("cuDeviceGet", C.byref(self.dev), 0)
        self.ctx = C.c_void_p()
        self._c("cuCtxCreate_v2", C.byref(self.ctx), 0, self.dev)
        sm = C.c_int()
        self._c("cuDeviceGetAttribute", C.byref(sm), 16, self.dev)  # MULTIPROCESSOR_COUNT
        self.sms = sm.value
        ptx = _ptx().encode() + b"\0"
        self.mod = C.c_void_p()
        log = C.create_string_buffer(8192)
        opts = (C.c_int * 2)(5, 6)  # CU_JIT_ERROR_LOG_BUFFER, ..._SIZE_BYTES
        vals = (C.c_void_p * 2)(C.cast(log, C.c_void_p), C.c_void_p(8192))
        r = self.lib.cuModuleLoadDataEx(C.byref(self.mod), ptx, 2, opts, vals)
        if r != 0:
            raise CudaError("cuModuleLoadDataEx: " + log.value.decode(errors="replace"), r)
        self.fn = {}
        for n in ("burn", "mfill", "mcheck"):
            f = C.c_void_p()
            self._c("cuModuleGetFunction", C.byref(f), self.mod, n.encode())
            self.fn[n] = f

    def close(self):
        try:
            for b in self.bufs:
                self.lib.cuMemFree_v2(C.c_uint64(b))
            self.bufs = []
            if self.ctx:
                self.lib.cuCtxDestroy_v2(self.ctx)
        finally:
            self.ctx = None

    def reopen(self):
        """After a fault the context is dead: throw it away and start over."""
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
        self._open()

    def alloc(self, nbytes):
        p = C.c_uint64()
        self._c("cuMemAlloc_v2", C.byref(p), C.c_size_t(nbytes))
        self.bufs.append(p.value)
        return p.value

    def free(self, p):
        self.lib.cuMemFree_v2(C.c_uint64(p))
        self.bufs.remove(p)

    def mem_info(self):
        f, t = C.c_size_t(), C.c_size_t()
        self._c("cuMemGetInfo_v2", C.byref(f), C.byref(t))
        return f.value, t.value

    def launch(self, name, grid, block, *args):
        holders = [C.c_uint64(a) if isinstance(a, Ptr) else C.c_uint32(a) for a in args]
        params = (C.c_void_p * len(holders))(*[C.cast(C.byref(h), C.c_void_p) for h in holders])
        self._c("cuLaunchKernel", self.fn[name], grid, 1, 1, block, 1, 1, 0, None, params, None)

    def sync(self):
        self._c("cuCtxSynchronize")

    def dtoh(self, dst, src, n):
        self._c("cuMemcpyDtoH_v2", dst, C.c_uint64(src), C.c_size_t(n))

    def memset32(self, p, v, n):
        self._c("cuMemsetD32_v2", C.c_uint64(p), C.c_uint(v), C.c_size_t(n))


class Ptr(int):
    """Marks a device pointer so it is passed as 64-bit."""


class CoreTest:
    """Deterministic compute load. golden() at stock clocks, then run() at any
    offset must reproduce the same checksum bit for bit."""

    BLOCK = 256

    def __init__(self, cuda, target_ms=120, blocks=None):
        self.cu = cuda
        self.blocks = blocks or cuda.sms * 16
        self.n = self.blocks * self.BLOCK
        self.out = Ptr(cuda.alloc(self.n * 4))
        self.host = (C.c_uint32 * self.n)()
        self.iters = 2000
        self.target_ms = target_ms
        self.ref = {}

    def _once(self, seed):
        self.cu.launch("burn", self.blocks, self.BLOCK, self.out, self.iters, seed)
        self.cu.sync()

    def calibrate(self):
        for _ in range(6):
            t = time.perf_counter()
            self._once(1)
            ms = (time.perf_counter() - t) * 1000
            if ms > 5:
                self.iters = max(1000, int(self.iters * self.target_ms / ms))
                if abs(ms - self.target_ms) < self.target_ms * 0.25:
                    break
            else:
                self.iters *= 8
        return self.iters

    def checksum(self):
        self.cu.dtoh(self.host, self.out, self.n * 4)
        return hashlib.blake2b(bytes(self.host), digest_size=16).hexdigest()

    SEEDS = (0x1234567, 0x0BADF00D, 0x5EED, 0x7777)

    def golden(self):
        """Run every seed twice at stock; both runs must agree (sanity check
        that the kernel itself is deterministic on this GPU)."""
        for s in self.SEEDS:
            self._once(s)
            a = self.checksum()
            self._once(s)
            b = self.checksum()
            if a != b:
                raise RuntimeError("GPU gave different results at STOCK clocks - not safe to tune "
                                   "(already unstable or a driver problem).")
            self.ref[s] = a

    def run(self, seconds, should_stop=lambda: False, on_tick=None):
        """Returns dict(ok, errors, launches, rate) - rate = chain-steps/s."""
        t0 = time.perf_counter()
        launches = errors = 0
        i = 0
        busy = 0.0
        while time.perf_counter() - t0 < seconds and not should_stop():
            s = self.SEEDS[i % len(self.SEEDS)]
            t = time.perf_counter()
            self._once(s)
            busy += time.perf_counter() - t
            launches += 1
            if self.checksum() != self.ref[s]:
                errors += 1
            i += 1
            if on_tick:
                on_tick(launches, errors)
        rate = launches * self.n * self.iters * 8 / busy if busy else 0
        return {"ok": errors == 0, "errors": errors, "launches": launches, "rate": rate}


class MemTest:
    """Fill/verify over most of free VRAM in 512 MiB chunks."""

    CHUNK = 512 << 20

    def __init__(self, cuda, fraction=0.80, max_bytes=None):
        self.cu = cuda
        free, _ = cuda.mem_info()
        want = int(free * fraction)
        if max_bytes:
            want = min(want, max_bytes)
        self.chunks = []
        while want >= self.CHUNK:
            try:
                self.chunks.append(Ptr(cuda.alloc(self.CHUNK)))
            except CudaError:
                break
            want -= self.CHUNK
        self.err = Ptr(cuda.alloc(4))
        self.n4 = self.CHUNK // 16
        self.blocks = cuda.sms * 8
        self.hosterr = (C.c_uint32 * 1)()

    @property
    def gib(self):
        return len(self.chunks) * self.CHUNK / (1 << 30)

    def run(self, seconds, should_stop=lambda: False, on_tick=None):
        t0 = time.perf_counter()
        bytes_moved = 0
        busy = 0.0
        errors = 0
        passes = 0
        seed = 0x9E37
        while time.perf_counter() - t0 < seconds and not should_stop():
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            self.cu.memset32(self.err, 0, 1)
            t = time.perf_counter()
            for k, p in enumerate(self.chunks):
                self.cu.launch("mfill", self.blocks, 256, p, self.n4, seed ^ k)
            self.cu.sync()
            for k, p in enumerate(self.chunks):
                self.cu.launch("mcheck", self.blocks, 256, p, self.n4, seed ^ k, self.err)
            self.cu.sync()
            busy += time.perf_counter() - t
            bytes_moved += 2 * len(self.chunks) * self.CHUNK
            self.cu.dtoh(self.hosterr, self.err, 4)
            errors += self.hosterr[0]
            passes += 1
            if on_tick:
                on_tick(passes, errors)
        bw = bytes_moved / busy / 1e9 if busy else 0
        return {"ok": errors == 0, "errors": errors, "passes": passes, "gbps": bw}

    def free(self):
        for p in self.chunks:
            self.cu.free(p)
        self.cu.free(self.err)
        self.chunks = []
