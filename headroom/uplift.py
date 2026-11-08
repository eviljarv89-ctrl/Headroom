"""Uplift maths. Two kinds of number are shown, never mixed up:

measured  - what Headroom's own tests saw on THIS PC (stock vs tuned), e.g.
            compute throughput, effective clock under full load, VRAM bandwidth,
            CPU benchmark score.
estimated - expected game FPS change, from the measured numbers scaled by how
            much games actually respond to clock (fitted to the review data
            in profiles/hardware.json).

GPU fit: KitGuru's Ventus 3X OC went ~2.82 -> ~3.15 GHz (+11.5%) with max
memory (+10.7% bandwidth) and gained 7-12% (mean 9.3%) in games.
  fps% ~= 0.65 * core_clock% + 0.20 * mem_bw%   ->  9.6%  (range 0.45/0.10 .. 0.85/0.25)

CPU fit (9800X3D): the 96 MB cache makes games far less clock-sensitive than
normal CPUs. Reviews show 0-3% from +200 MHz / CO in CPU-bound games and
about 0-1% when the GPU is the limit (1440p/4K).
  cpu-bound fps% ~= 0.5 * single_thread%   gpu-bound fps% ~= 0.1 * single_thread%
"""


def pct(new, old):
    if not old:
        return 0.0
    return (new / old - 1.0) * 100.0


def gpu(baseline, tuned):
    """baseline/tuned: dicts with eff_clk, rate, gbps, power, temp."""
    core = pct(tuned.get("eff_clk", 0), baseline.get("eff_clk", 0))
    mem = pct(tuned.get("gbps", 0), baseline.get("gbps", 0))
    compute = pct(tuned.get("rate", 0), baseline.get("rate", 0))
    est = 0.65 * core + 0.20 * mem
    lo = 0.45 * core + 0.10 * mem
    hi = 0.85 * core + 0.25 * mem
    return {
        "measured": {
            "core_clock_pct": core, "mem_bandwidth_pct": mem, "compute_pct": compute,
            "power_w": (baseline.get("power"), tuned.get("power")),
            "temp_c": (baseline.get("temp"), tuned.get("temp")),
        },
        "estimated_games_pct": est, "estimated_range_pct": (lo, hi),
    }


def cpu(baseline, tuned):
    st = pct(tuned.get("single", 0), baseline.get("single", 0))
    mt = pct(tuned.get("multi", 0), baseline.get("multi", 0))
    return {
        "measured": {"single_thread_pct": st, "multi_thread_pct": mt,
                     "max_clock": (baseline.get("max_clock"), tuned.get("max_clock")),
                     "package_w": (baseline.get("package_w"), tuned.get("package_w")),
                     "tdie_c": (baseline.get("tdie"), tuned.get("tdie"))},
        "estimated_cpu_bound_games_pct": 0.5 * st,
        "estimated_gpu_bound_games_pct": 0.1 * st,
    }


def predict_gpu(core_offset, mem_offset_reported, stock_eff_clk=2820.0, power_capped=True):
    """Before testing: what a given offset should be worth. Under the 300 W cap
    the card can't hold the full offset at heavy load; reviews show about 80% of
    it (+420 -> about +330 MHz real)."""
    real = core_offset * (0.8 if power_capped else 1.0)
    core = real / stock_eff_clk * 100
    mem = mem_offset_reported / 14001.0 * 100
    return {"core_clock_pct": core, "mem_bandwidth_pct": mem,
            "estimated_games_pct": 0.65 * core + 0.20 * mem}


def predict_cpu(boost_override_mhz, co_avg, stock_fmax=5200.0):
    """Single-thread: the boost override raises fmax directly; CO mainly lets the
    chip hold it under load. Multi-thread on a 9800X3D is power/temperature
    limited: about 0.12% per CO point (forum CB R23 data: -20..-30 -> +1-4%)."""
    st = boost_override_mhz / stock_fmax * 100 * 0.9
    mt = min(4.5, abs(co_avg) * 0.12)
    return {"single_thread_pct": st, "multi_thread_pct": mt,
            "estimated_cpu_bound_games_pct": 0.5 * st, "estimated_gpu_bound_games_pct": 0.1 * st}
