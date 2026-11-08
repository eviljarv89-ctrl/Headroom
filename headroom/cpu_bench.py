"""Quick CPU benchmark used for before/after uplift. Pure Python integer, float
and dict work: it scales with core clock like most game logic does. It is
repeated 3 times and the median kept (spread is about 1%), so treat changes
under 1% as noise. For headline numbers, also run Cinebench / 3DMark CPU and
type the scores into the Benchmarks tab."""
import multiprocessing as mp
import os
import statistics
import time


def _chunk():
    x = 1
    f = 1.0001
    d = {}
    for i in range(3000):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        f = f * 1.0000001 + (x & 255) * 1e-9
        d[x & 1023] = i
    return x ^ len(d) ^ int(f)


def _worker(seconds):
    end = time.perf_counter() + seconds
    n = 0
    while time.perf_counter() < end:
        _chunk()
        n += 1
    return n / seconds


def single(seconds=6, reps=3):
    return statistics.median(_worker(seconds) for _ in range(reps))


def multi(seconds=6, reps=3, procs=None):
    procs = procs or os.cpu_count() or 16
    ctx = mp.get_context("spawn")
    scores = []
    with ctx.Pool(procs) as pool:
        pool.map(_worker, [0.5] * procs)  # warm-up / process start
        for _ in range(reps):
            scores.append(sum(pool.map(_worker, [seconds] * procs)))
    return statistics.median(scores)


def run(tele, log, progress=lambda *a: None):
    """Returns {single, multi, max_clock, package_w, tdie}; samples LHM while it runs."""
    peak = {"clk": 0, "w": 0, "t": 0}

    def sample():
        c = tele.sample().get("cpu") or {}
        peak["clk"] = max(peak["clk"], c.get("max_core_clock") or 0)
        peak["w"] = max(peak["w"], c.get("package_w") or 0)
        peak["t"] = max(peak["t"], c.get("tdie") or 0)

    import threading
    stop = threading.Event()

    def loop():
        while not stop.wait(1):
            sample()
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    try:
        progress("bench", "Single-thread benchmark (3 x 6 s)")
        st = single()
        st_clk = peak["clk"]
        progress("bench", "Multi-thread benchmark, 16 threads (3 x 6 s)")
        mt = multi()
    finally:
        stop.set()
        th.join(2)
    r = {"single": st, "multi": mt, "max_clock": st_clk, "package_w": peak["w"], "tdie": peak["t"],
         "when": time.strftime("%Y-%m-%d %H:%M")}
    log("CPU bench: single %.0f, multi %.0f, peak clock %.0f MHz, %.0f W, %.1f C" %
        (st, mt, st_clk, peak["w"], peak["t"]))
    return r
