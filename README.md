# Headroom

Safe, automatic overclocking for Windows gaming PCs: a GPU auto-overclock with its own stress tests, a GPU fan curve, and a guided AMD Ryzen Curve Optimizer tune. It measures the speed-up before and after, and works out what that means for games.

**Install:** run `HeadroomSetup-<version>.exe`. It asks for admin rights, because changing clocks needs them. When it finishes, Headroom starts, scans the hardware and builds a profile for that exact PC. No Python or other software is needed.

## What it does

| | How | Limits that are never crossed |
|---|---|---|
| **GPU** (NVIDIA RTX) | Searches core and memory clock offsets through NVIDIA's NVML. Each step is checked bit for bit against a stock run by Headroom's own CUDA stress test, and driver resets count as failures. A combined soak and a safety margin come after the search. | No voltage or power-limit changes. Back to stock at the core or memory-junction temperature limit (default 83 °C / 96 °C). Offset caps per GPU family. |
| **GPU fans** | A fan curve with separate core-temperature and VRAM-temperature curves; the fans run at whichever asks for more. The card's own fan control (including zero-rpm at idle) takes over when it's cool. | Forced 100 % at 80 °C core / 94 °C VRAM. Fans go back to the card's control when the curve is turned off, on reset and on exit. |
| **CPU** (AMD Ryzen 5000/7000/9000) | PBO + per-core negative Curve Optimizer. CoreCycler finds the per-core values; Headroom shows the exact BIOS sheet, then verifies and measures. | Power limits, Vcore and SoC voltage are never raised. The stress test stops on the temperature limit, or if the chosen pump / CPU fan headers slow down. |
| **RAM** | Advice only (e.g. "EXPO looks off", "4 sticks at this speed is already past the rating"). | Never changed. |

## Hardware profiles

On first start Headroom scans the PC: CPU, motherboard and BIOS, GPU and its board partner, driver and power limits, memory kits, sensors and fans. It then builds the active profile from the best source it has for each part:

1. **Research with Claude** (Setup tab, optional). Claude searches reviews and owner results for the exact parts, then writes a profile with source links. It runs through [Claude Code](https://claude.com/claude-code) with the login already on the PC, so it counts against your Claude plan; no API key is needed. Without Claude Code, an Anthropic API key works instead (stored encrypted with Windows DPAPI, billed to that account). Only model names, versions and limits from the scan are sent. Only URLs that were actually retrieved are kept as sources.
2. **Built-in research** (`profiles/builtin/`), for parts that have already been researched.
3. **Family guidance** (`profiles/families.json`): conservative ranges for each GPU or CPU generation.

Whatever the source, `headroom/profiles.py` clamps the safety limits to hard caps. Headroom's own tests decide the final values.

## Requirements

- Windows 10/11 64-bit with the Microsoft Edge WebView2 Runtime (already part of Windows 11).
- Sensors: CPU temperature, GPU memory temperature and fan speeds come from [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) with its web server on port 8085. The Setup tab can download and configure it. Recent versions need the [PawnIO](https://pawnio.eu) driver.
- CPU tuning downloads [CoreCycler](https://github.com/sp00n/CoreCycler) when you press *Install CoreCycler*.

## Building from source

```powershell
# needs 64-bit Python 3.12 (python.org or `winget install Python.Python.3.12`)
powershell -ExecutionPolicy Bypass -File installer\build.ps1 -Version 1.0.0
# -> dist\HeadroomSetup-1.0.0.exe (+ .sha256)
```

The build script downloads the WebView2 SDK from NuGet and compiles `Headroom.exe` and the installer with the C# compiler that ships with Windows. It also copies a private Python runtime (isolated with a `._pth` file) and installs the Anthropic SDK into `vendor/` from `installer/requirements.txt`. Every download is pinned by SHA-256, and the build stops on a mismatch.

To run from a checkout: build once (for the WebView2 DLLs and `Headroom.exe`), then
`py -3.12 -m pip install --require-hashes --no-deps -r installer\requirements.txt --target vendor` and start `Headroom.exe`.

Installer switches: `/S` silent, `/D=C:\path` install folder, `/extract <dir>` unpack only.
Uninstall from *Settings → Apps*, or run `Uninstall.exe /uninstall [/S] [/removedata]`. Uninstalling puts the GPU back to stock, gives the fans back to the card and removes Headroom's scheduled tasks.

## Layout

```
Headroom.cs            WebView2 window + launcher (elevated), starts the Python backend
headroom_server.pyw    backend entry: API + UI on 127.0.0.1:7871
headroom_fans.pyw      fan-curve process (keeps running with the window closed)
headroom_apply.pyw     applies the saved GPU profile at logon, skipped after a crash
headroom_reset.pyw     GPU to stock + fans to auto (used by the uninstaller)
headroom/              scan, profiles, research, gpu_tune, cpu_tune, fan_curve, nvml, cuda_stress, sensors, uplift
web/index.html         the UI
profiles/              builtin/ researched profiles, families.json fallback guidance
installer/             Setup.cs (installer + uninstaller), build.ps1, setup.manifest
```

Data lives in `%APPDATA%\Headroom` (profile, scan, state, logs). Downloaded tools go in `tools\` inside the install folder, which only administrators can change, because they run elevated.

## Security

See [SECURITY.md](SECURITY.md) for how the elevated backend is protected and how to report a vulnerability.

## Disclaimer

Overclocking runs hardware outside its factory settings. Headroom is conservative by design, but crashes or freezes during testing are expected, and some manufacturers don't cover overclocking under warranty. Use at your own risk (see LICENSE).
