"""'Research my hardware': uses Claude with web search to find reviews, community
overclocking results, safety limits and exact BIOS menu names for the parts in
THIS PC, the same way Headroom's built-in profiles were researched.

Runs through Claude Code (the `claude` CLI) with the login already on this PC, so
it uses the user's Claude plan. Without Claude Code it falls back to the user's own
Anthropic API key (stored encrypted with Windows DPAPI, or ANTHROPIC_API_KEY).
Nothing is sent except the hardware scan summary below (no serials, user names or files).

The CLI runs with only its WebSearch and WebFetch tools, no user settings, hooks or
MCP servers, in an empty folder, so web pages it reads can't make it do anything else.
Because the backend is elevated, only a claude.exe signed by Anthropic is used.

Two steps:
  1. research  - Claude searches/reads the web and writes notes. Every URL it
                 actually saw is collected from the search/fetch results.
  2. structure - the notes become a profile via structured JSON output. Sources
                 not seen in step 1 are dropped, so links can't be made up.
Safety limits from research are clamped by profiles.py like any other source.
"""
import base64
import ctypes
import ctypes.wintypes as W
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from .common import SETTINGS_FILE, read_json, write_json

MODEL = "claude-opus-5-5"
BETAS = ["server-side-fallback-2026-07-01"]   # fallbacks="default": a declined request is retried on another model
TOOLS = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 20},
         {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 12}]


class ResearchError(RuntimeError):
    pass


# ---------------------------------------------------------------- API key (DPAPI)
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", W.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    crypt = ctypes.windll.crypt32
    src = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    dst = _Blob()
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst)):   # 0x1 = UI forbidden
        raise ResearchError("Windows couldn't %s the API key" % ("encrypt" if protect else "decrypt"))
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def save_key(key):
    s = read_json(SETTINGS_FILE, {}) or {}
    if key:
        s["anthropic_key"] = base64.b64encode(_dpapi(key.strip().encode(), True)).decode()
    else:
        s.pop("anthropic_key", None)
    write_json(SETTINGS_FILE, s)


def load_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    blob = (read_json(SETTINGS_FILE, {}) or {}).get("anthropic_key")
    if not blob:
        return None
    try:
        return _dpapi(base64.b64decode(blob), False).decode()
    except Exception:  # noqa: BLE001
        return None


def has_key():
    return bool(load_key())


# ---------------------------------------------------------------- prompts
def scan_summary(scan):
    """Only what research needs: model names, IDs, versions. No serials or user data."""
    c, b, g, m = scan.get("cpu") or {}, scan.get("board") or {}, scan.get("gpu") or {}, scan.get("memory") or {}
    nv = g.get("nvidia") or {}
    return {
        "cpu": c.get("name"), "cpu_family": c.get("family"),
        "motherboard": "%s %s" % (b.get("maker") or "", b.get("model") or ""), "bios_version": b.get("bios"),
        "bios_date": b.get("bios_date"),
        "gpu": g.get("name"), "gpu_board_partner": g.get("board_vendor") or None,
        "gpu_pci_subsystem_vendor": g.get("board_vendor_id"), "gpu_vbios": nv.get("vbios"),
        "gpu_driver": nv.get("driver"), "gpu_power_limits_w": nv.get("power"), "gpu_max_clocks_mhz": nv.get("max_clock_mhz"),
        "gpu_vram_gb": nv.get("vram_gb"),
        "memory": {"kits": m.get("kits"), "sticks": m.get("sticks"), "running_mts": m.get("running_mts"), "ddr": m.get("ddr")},
        "os": (scan.get("os") or {}).get("name"),
    }


SYSTEM = """You research PC hardware for Headroom, a safety-first overclocking tool. Headroom only does:
- NVIDIA GPUs: core and memory clock offsets (no voltage changes, power limit left as the card allows), found by its own stress test.
- AMD Ryzen CPUs: PBO with stock power limits, a Max Boost Clock Override, and per-core negative Curve Optimizer found by CoreCycler. The user enters BIOS settings by hand from a sheet Headroom shows.
It never raises voltages or power limits and doesn't tune RAM.

Your findings set the starting points, expectations and safety limits Headroom uses, so accuracy matters more than completeness:
- Prefer professional reviews (TechPowerUp, Tom's Hardware, Guru3D, KitGuru, Hardware Unboxed, Gamers Nexus, ComputerBase, Igor's Lab), manufacturer spec pages and manuals, and well-documented community results (Overclock.net, r/overclocking).
- Match the exact model. A GPU's board partner and cooler (from the PCI subsystem vendor) matter: power limits and memory cooling differ between partner cards of the same GPU.
- When something isn't found, say so plainly. Never estimate a number and present it as a finding."""

RESEARCH_TASK = """Research overclocking and safety information for the parts in this PC:

{scan}

Find and report, with the source for each fact:
1. GPU: stock boost clock, memory type/speed, power limit (default and maximum) for THIS partner card, stable core / memory offsets that reviewers and owners reached (in MSI Afterburner units), the gains they measured in games, and safe GPU core and memory junction temperatures.
2. CPU: stock clocks and power limits (PPT/TDC/EDC or PL1/PL2), Tjmax, whether PBO / Curve Optimizer / boost override are available on this exact CPU, typical stable per-core Curve Optimizer values, measured gains, and known safety issues (e.g. SoC voltage limits, microcode or BIOS advisories).
3. Motherboard: the exact BIOS menu path and setting names for PBO, boost override, Curve Optimizer (per core) and the platform thermal limit on this board's current BIOS family; any BIOS versions to avoid or install.
4. Memory: whether this kit combination at the current speed is within the CPU maker's rating, and known issues.
Keep the notes factual and compact."""

STRUCTURE_TASK = """Turn these research notes into Headroom's profile JSON for this PC.

Hardware scan:
{scan}

Research notes:
{notes}

Sources that were actually retrieved (use ONLY these URLs, copied exactly; leave a url empty if none fits):
{sources}

Rules: use null for anything the notes don't support. Temperatures in C. GPU memory offsets in MSI Afterburner units. Curve Optimizer values are negative numbers. Expected uplift as percentages."""

_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_RANGE = {"type": "array", "items": {"type": "number"}}
_RESULTS = {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                       "required": ["source", "settings", "result", "url"],
                                       "properties": {"source": {"type": "string"}, "settings": {"type": "string"},
                                                      "result": {"type": "string"}, "url": {"type": "string"}}}}
_STRS = {"type": "array", "items": {"type": "string"}}


def _obj(props):
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


SCHEMA = _obj({
    "gpu": _obj({
        "board": _STR, "chip": _STR, "summary": {"type": "string"},
        "stock": _obj({"boost_mhz": _NUM, "mem_gbps": _NUM, "power_w": _NUM, "power_max_w": _NUM}),
        "community_results": _RESULTS,
        "core_offset_mhz": _RANGE, "mem_offset_afterburner": _RANGE,
        "games_uplift_pct": _RANGE, "games_uplift_typical_pct": _NUM,
        "max_safe_core_c": _NUM, "max_safe_mem_junction_c": _NUM,
        "notes": _STRS, "sources": _STRS}),
    "cpu": _obj({
        "arch": _STR, "summary": {"type": "string"},
        "stock": _obj({"base_mhz": _NUM, "boost_mhz": _NUM, "tdp_w": _NUM, "ppt_w": _NUM, "tjmax_c": _NUM}),
        "pbo_available": {"type": "boolean"}, "curve_optimizer_available": {"type": "boolean"},
        "boost_override_available": {"type": "boolean"},
        "community_results": _RESULTS, "co_per_core": _RANGE, "boost_override_mhz": _NUM,
        "single_thread_uplift_pct": _RANGE, "multi_thread_uplift_pct": _RANGE, "games_uplift_pct": _RANGE,
        "max_safe_temp_c": _NUM, "notes": _STRS, "sources": _STRS}),
    "board": _obj({
        "bios_key": _STR, "pbo_path": _STR,
        "settings_to_enter": {"type": "array", "items": _obj({"setting": {"type": "string"}, "value": {"type": "string"}})},
        "bios_advice": _STRS, "sources": _STRS}),
    "memory": _obj({"verdict": {"type": "string"}, "notes": _STRS, "sources": _STRS}),
})


# ---------------------------------------------------------------- helpers
def _dump(block):
    try:
        return block.to_dict()
    except AttributeError:
        return block if isinstance(block, dict) else {}


def _collect_urls(obj, out):
    if isinstance(obj, dict):
        u = obj.get("url")
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            out.setdefault(u, obj.get("title") or "")
        for v in obj.values():
            _collect_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_urls(v, out)


def _check_stop(msg, step):
    if msg.stop_reason == "refusal":
        cat = getattr(getattr(msg, "stop_details", None), "category", None)
        raise ResearchError("Claude declined the %s step%s." % (step, " (%s)" % cat if cat else ""))


def _rng(v, default=None):
    if isinstance(v, list) and len(v) >= 2 and all(isinstance(x, (int, float)) for x in v[:2]):
        return [v[0], v[1]]
    return default


def _keep(urls, allowed):
    return [u for u in (urls or []) if u in allowed]


def to_profiles(j, allowed):
    """Structured answer -> Headroom's profile schema (same shape as profiles/builtin)."""
    g, c, b, m = j.get("gpu") or {}, j.get("cpu") or {}, j.get("board") or {}, j.get("memory") or {}
    res = lambda rows: [dict(r, url=r["url"] if r.get("url") in allowed else "") for r in rows or []]  # noqa: E731
    gpu = {
        "board": g.get("board"), "chip": g.get("chip"), "summary": g.get("summary"),
        "stock": {k: v for k, v in (g.get("stock") or {}).items() if v is not None},
        "tuning_method": "Core + memory clock offsets via NVML (no voltage change; the card keeps its own power limit).",
        "community_results": res(g.get("community_results")),
        "typical": {"core_offset_mhz": _rng(g.get("core_offset_mhz"), [0, 0]),
                    "mem_offset_afterburner": _rng(g.get("mem_offset_afterburner"), [0, 0])},
        "expected_uplift_pct": {"games": _rng(g.get("games_uplift_pct"), [0, 0]),
                                "games_typical": g.get("games_uplift_typical_pct") or 0},
        "safety": {
            "abort_core_c": min(83, (g.get("max_safe_core_c") or 88) - 5),
            "abort_mem_junction_c": min(100, (g.get("max_safe_mem_junction_c") or 105) - 9),
            # the search may go a bit past what owners reached, never past the hard caps (profiles.CAP)
            "max_core_offset": int(max(_rng(g.get("core_offset_mhz"), [0, 150])[1] * 1.15, 50)),
            "max_mem_offset_reported_mhz": int(max(_rng(g.get("mem_offset_afterburner"), [0, 500])[1] * 1.1, 0)),
            "notes": g.get("notes") or []},
        "sources": _keep(g.get("sources"), allowed),
    }
    tj = (c.get("stock") or {}).get("tjmax_c") or c.get("max_safe_temp_c")
    cpu = {
        "arch": c.get("arch"), "summary": c.get("summary"),
        "stock": {k: v for k, v in (c.get("stock") or {}).items() if v is not None},
        "tuning_method": ("PBO + per-core negative Curve Optimizer (CoreCycler)" + (" + Max Boost Clock Override"
                          if c.get("boost_override_available") else "") + ". Power limits stay stock.")
        if c.get("curve_optimizer_available") else "No Curve Optimizer on this CPU: monitoring and benchmark only.",
        "community_results": res(c.get("community_results")),
        "typical": {"co_per_core": _rng(c.get("co_per_core"), [0, 0]),
                    "boost_override_mhz": int(c.get("boost_override_mhz") or 0) if c.get("boost_override_available") else 0},
        "expected_uplift_pct": {"single_thread": _rng(c.get("single_thread_uplift_pct"), [0, 0]),
                                "multi_thread": _rng(c.get("multi_thread_uplift_pct"), [0, 0]),
                                "games_cpu_bound": _rng(c.get("games_uplift_pct"), [0, 0]), "games_gpu_bound": [0, 1]},
        "safety": {"abort_tdie_c": (tj - 5) if tj else 90, "platform_thermal_limit_c": min(85, (tj - 10) if tj else 85),
                   "notes": c.get("notes") or []},
        "sources": _keep(c.get("sources"), allowed),
    }
    board = {
        "bios_key": b.get("bios_key"), "pbo_path": b.get("pbo_path"),
        "settings_to_enter": [[r["setting"], r["value"]] for r in b.get("settings_to_enter") or []],
        "also": " ".join(b.get("bios_advice") or []), "sources": _keep(b.get("sources"), allowed),
    }
    memory = {"verdict": m.get("verdict"), "notes": m.get("notes") or [], "sources": _keep(m.get("sources"), allowed)}
    return {"gpu": gpu, "cpu": cpu, "board": board, "memory": memory}


# ---------------------------------------------------------------- Anthropic API (fallback)
def run_api(scan, progress=lambda msg: None, key=None):
    """Returns {"gpu","cpu","board","memory","sources","when","model","usage"}. Raises ResearchError."""
    try:
        import anthropic
    except ImportError as e:
        raise ResearchError("The Anthropic SDK is missing from this install (%s)." % e) from e
    key = key or load_key()
    if not key:
        raise ResearchError("No Anthropic API key. Add one in Setup (console.anthropic.com > API keys).")
    client = anthropic.Anthropic(api_key=key, timeout=900.0, max_retries=3)
    summary = json.dumps(scan_summary(scan), indent=1)
    urls, notes, usage = {}, [], {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "web_fetches": 0}

    def add_usage(msg):
        u = msg.usage
        usage["input_tokens"] += (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
        usage["output_tokens"] += u.output_tokens or 0
        st = getattr(u, "server_tool_use", None)
        if st:
            usage["web_searches"] += getattr(st, "web_search_requests", 0) or 0
            usage["web_fetches"] += getattr(st, "web_fetch_requests", 0) or 0

    try:
        # ---- step 1: research with web search, resuming on pause_turn
        progress("Researching your parts on the web (takes a few minutes)")
        messages = [{"role": "user", "content": RESEARCH_TASK.format(scan=summary)}]
        searches = fetches = 0
        for _ in range(6):
            with client.beta.messages.stream(
                    model=MODEL, max_tokens=64000, betas=BETAS, fallbacks="default",
                    system=SYSTEM, thinking={"type": "adaptive"}, output_config={"effort": "high"},
                    tools=TOOLS, messages=messages) as stream:
                for ev in stream:
                    if ev.type == "content_block_start":
                        t = ev.content_block.type
                        if t == "web_search_tool_result":
                            searches += 1
                            progress("Researching: %d searches, %d pages read" % (searches, fetches))
                        elif t == "web_fetch_tool_result":
                            fetches += 1
                            progress("Researching: %d searches, %d pages read" % (searches, fetches))
                msg = stream.get_final_message()
            add_usage(msg)
            _check_stop(msg, "research")
            blocks = [_dump(b) for b in msg.content]
            for bl in blocks:
                if bl.get("type") in ("web_search_tool_result", "web_fetch_tool_result") or bl.get("citations"):
                    _collect_urls(bl, urls)
                if bl.get("type") == "text" and bl.get("text"):
                    notes.append(bl["text"])
            if msg.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": msg.content}]
        if not notes:
            raise ResearchError("The research step returned no findings.")

        # ---- step 2: notes -> profile JSON
        progress("Building your profile from %d sources" % len(urls))
        src_list = "\n".join("- %s  (%s)" % (u, t) for u, t in urls.items()) or "(none)"
        msg = client.beta.messages.create(
            model=MODEL, max_tokens=16000, betas=BETAS, fallbacks="default",
            system=SYSTEM, output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": STRUCTURE_TASK.format(scan=summary, notes="\n\n".join(notes), sources=src_list)}])
        add_usage(msg)
        _check_stop(msg, "profile")
        if msg.stop_reason == "max_tokens":
            raise ResearchError("The profile answer was cut off; try again.")
        text = next((b.text for b in msg.content if b.type == "text"), "")
        j = json.loads(text)
    except ResearchError:
        raise
    except anthropic.AuthenticationError as e:
        raise ResearchError("The API key was not accepted.") from e
    except anthropic.PermissionDeniedError as e:
        raise ResearchError("This API key can't use %s or web search (%s)." % (MODEL, e.message)) from e
    except anthropic.RateLimitError as e:
        raise ResearchError("Rate limited by the Anthropic API; try again in a minute.") from e
    except anthropic.APIStatusError as e:
        raise ResearchError("Anthropic API error %s: %s" % (e.status_code, e.message)) from e
    except anthropic.APIConnectionError as e:
        raise ResearchError("Couldn't reach the Anthropic API (check the internet connection).") from e
    except ValueError as e:
        raise ResearchError("The profile answer wasn't valid JSON (%s)." % e) from e

    allowed = set(urls)
    out = to_profiles(j, allowed)
    usage["via"] = "Anthropic API key"
    out.update(sources=sorted(allowed), when=time.strftime("%Y-%m-%d %H:%M"), model=MODEL, usage=usage,
               notes_text="\n\n".join(notes))
    return out


# ---------------------------------------------------------------- Claude Code CLI
CLI_TIMEOUT = 1800          # whole research step, seconds
_NO_WINDOW = 0x08000000     # CREATE_NO_WINDOW: claude.exe is a console app, the backend has no console
_cli_cache = {"t": 0, "v": None, "busy": False, "checked": False}


def _cli_candidates():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    seen = []
    for p in (shutil.which("claude.exe"), os.path.join(home, ".local", "bin", "claude.exe"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "claude", "claude.exe")):
        if p and os.path.isfile(p) and os.path.normcase(os.path.abspath(p)) not in seen:
            seen.append(os.path.normcase(os.path.abspath(p)))
            yield os.path.abspath(p)


def _signed_by_anthropic(path):
    """Authenticode check: the backend runs elevated, and claude.exe lives in a folder the user can write."""
    ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    cmd = ("$s = Get-AuthenticodeSignature -LiteralPath $env:HR_CLI; "
           "if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match '^CN=\"?Anthropic, PBC\"?,') { 'ok' }")
    try:
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True,
                           timeout=30, creationflags=_NO_WINDOW, env=dict(os.environ, HR_CLI=path))
        return r.stdout.strip() == "ok"
    except (OSError, subprocess.SubprocessError):
        return False


def _cli_env():
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "HEADROOM_TOKEN"):
        env.pop(k, None)    # use the CLI's own login, never Headroom's key or token
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def _probe_cli():
    for path in _cli_candidates():
        if not _signed_by_anthropic(path):
            continue
        v = {"path": path, "logged_in": False, "account": None}
        try:
            r = subprocess.run([path, "auth", "status"], capture_output=True, text=True, timeout=30,
                               creationflags=_NO_WINDOW, env=_cli_env(), cwd=tempfile.gettempdir())
            j = json.loads(r.stdout or "{}")
            v["logged_in"] = bool(j.get("loggedIn"))
            v["account"] = j.get("authMethod")
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return v
    return None


def _refresh_cli():
    try:
        v = _probe_cli()
        _cli_cache.update(t=time.time(), v=v, checked=True)
    finally:
        _cli_cache["busy"] = False


def cli_status(fresh=False):
    """{"path", "logged_in", "account"} for a usable claude.exe, or None.
    fresh=True checks now (blocking); otherwise the cached answer is returned and refreshed
    in the background once it's a minute old, so the UI's status poll never waits on it."""
    if fresh:
        _refresh_cli()
        return _cli_cache["v"]
    if time.time() - _cli_cache["t"] > 60 and not _cli_cache.get("busy"):
        _cli_cache["busy"] = True
        threading.Thread(target=_refresh_cli, daemon=True).start()
    return _cli_cache["v"]


def cli_checked():
    return bool(_cli_cache.get("checked"))


def _cli_args(path, *extra):
    return [path, "-p", "--model", MODEL, "--strict-mcp-config", "--setting-sources", "",
            "--no-session-persistence", "--disable-slash-commands", "--system-prompt", SYSTEM, *extra]


def _tool_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def _search_links(text, urls):
    """WebSearch results carry 'Links: [{"title", "url"}, ...]'."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"Links:\s*(?=\[)", text):
        try:
            links, _ = dec.raw_decode(text, m.end())
        except ValueError:
            continue
        _collect_urls(links, urls)


def _run_cli_research(path, summary, progress, workdir):
    args = _cli_args(path, "--output-format", "stream-json", "--verbose", "--effort", "high",
                     "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
                     "--permission-mode", "dontAsk")
    errf = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf,
                         cwd=workdir, env=_cli_env(), creationflags=_NO_WINDOW, text=True, encoding="utf-8")
    timer = threading.Timer(CLI_TIMEOUT, p.kill)
    timer.start()
    urls, pending, result = {}, {}, None
    searches = fetches = 0
    try:
        p.stdin.write(RESEARCH_TASK.format(scan=summary))
        p.stdin.close()
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            content = (ev.get("message") or {}).get("content")
            if t == "assistant" and isinstance(content, list):
                for c in content:
                    if c.get("type") == "tool_use":
                        pending[c.get("id")] = (c.get("name"), c.get("input") or {})
                        if c.get("name") == "WebSearch":
                            searches += 1
                        elif c.get("name") == "WebFetch":
                            fetches += 1
                        progress("Researching: %d searches, %d pages read" % (searches, fetches))
            elif t == "user" and isinstance(content, list):
                for c in content:
                    if c.get("type") != "tool_result" or c.get("is_error"):
                        continue
                    name, inp = pending.pop(c.get("tool_use_id"), (None, {}))
                    if name == "WebSearch":
                        _search_links(_tool_text(c.get("content")), urls)
                    elif name == "WebFetch":
                        _collect_urls({"url": inp.get("url")}, urls)
            elif t == "result":
                result = ev
        p.wait()
    finally:
        timer.cancel()
        if p.poll() is None:
            p.kill()
    errf.seek(0)
    err = errf.read().strip()
    errf.close()
    if result is None:
        raise ResearchError("Claude Code stopped without an answer%s." % (": " + err[-300:] if err else
                            " (it ran longer than %d minutes)" % (CLI_TIMEOUT // 60)))
    if result.get("is_error"):
        raise ResearchError(_cli_error(result))
    return result, urls, searches, fetches


def _cli_error(result):
    msg = str(result.get("result") or result.get("errors") or result.get("subtype") or "unknown error")
    if re.search(r"authenticat|log ?in|oauth|expired", msg, re.I):
        return "Claude Code isn't logged in. Run 'claude' in a terminal and use /login, then try again."
    if re.search(r"usage limit|rate limit|limit reached", msg, re.I):
        return "Your Claude plan's usage limit was reached; try again later. (%s)" % msg[:200]
    return "Claude Code error: %s" % msg[:300]


def _run_cli_structure(path, prompt, workdir):
    args = _cli_args(path, "--output-format", "json", "--effort", "medium", "--tools", "",
                     "--json-schema", json.dumps(SCHEMA, separators=(",", ":")))
    try:
        r = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=900,
                           cwd=workdir, env=_cli_env(), creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        raise ResearchError("Building the profile took too long; try again.") from e
    try:
        res = json.loads(r.stdout)
    except ValueError as e:
        raise ResearchError("Claude Code gave no answer for the profile step: %s" % (r.stderr or r.stdout)[-300:]) from e
    if res.get("is_error"):
        raise ResearchError(_cli_error(res))
    j = res.get("structured_output")
    if not isinstance(j, dict):
        raise ResearchError("The profile answer didn't match the expected format; try again.")
    return res, j


def run_cli(scan, progress=lambda msg: None, cli=None):
    cli = cli or cli_status(fresh=True)
    if not cli:
        raise ResearchError("Claude Code isn't installed (or its claude.exe isn't signed by Anthropic).")
    if not cli["logged_in"]:
        raise ResearchError("Claude Code isn't logged in. Run 'claude' in a terminal and use /login, then try again.")
    summary = json.dumps(scan_summary(scan), indent=1)
    workdir = tempfile.mkdtemp(prefix="headroom-research-")
    try:
        progress("Researching your parts on the web with Claude Code (takes a few minutes)")
        res1, urls, searches, fetches = _run_cli_research(cli["path"], summary, progress, workdir)
        notes = (res1.get("result") or "").strip()
        if not notes:
            raise ResearchError("The research step returned no findings.")
        progress("Building your profile from %d sources" % len(urls))
        src_list = "\n".join("- %s  (%s)" % (u, t) for u, t in urls.items()) or "(none)"
        res2, j = _run_cli_structure(cli["path"], STRUCTURE_TASK.format(scan=summary, notes=notes, sources=src_list), workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": searches, "web_fetches": fetches,
             "cost_usd_equiv": round((res1.get("total_cost_usd") or 0) + (res2.get("total_cost_usd") or 0), 4),
             "via": "Claude Code"}
    for res in (res1, res2):
        u = res.get("usage") or {}
        usage["input_tokens"] += sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        usage["output_tokens"] += u.get("output_tokens") or 0
    allowed = set(urls)
    out = to_profiles(j, allowed)
    out.update(sources=sorted(allowed), when=time.strftime("%Y-%m-%d %H:%M"), model=MODEL, usage=usage, notes_text=notes)
    return out


def available():
    """What 'Research my hardware' will use: "cli", "key" or None."""
    c = cli_status()
    if c and c["logged_in"]:
        return "cli"
    return "key" if has_key() else None


def run(scan, progress=lambda msg: None):
    """Claude Code login first, the API key second. Raises ResearchError."""
    c = cli_status(fresh=True)
    if c and c["logged_in"]:
        return run_cli(scan, progress, c)
    if has_key():
        return run_api(scan, progress)
    if c:
        raise ResearchError("Claude Code isn't logged in. Run 'claude' in a terminal and use /login, then try again.")
    raise ResearchError("Install Claude Code and log in (or add an Anthropic API key) to use research.")
