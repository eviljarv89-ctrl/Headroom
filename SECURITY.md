# Security policy

Headroom runs with administrator rights and changes hardware settings, so security reports are taken seriously.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting instead: the repository's **Security** tab → **Report a vulnerability**. Include the version, what an attacker could do, and steps to reproduce. You should get a reply within a week.

## Supported versions

Only the latest release gets security fixes.

## Security design

- **Local API:** the backend listens only on `127.0.0.1`. Every API request needs a random per-launch token that only the Headroom window knows (it is passed to the backend in its environment, never written to disk). Requests whose `Host` or `Origin` isn't Headroom's are refused, which blocks web pages (CSRF, DNS rebinding) and non-admin programs.
- **UI:** a strict Content-Security-Policy (script nonce) stops injected markup from running script. Profile text from web research is stripped of markup, and only `https` links are kept. The window never navigates away from Headroom's own page, and it only opens `http(s)` links, in the normal browser.
- **Files that run elevated** (the app, its Python runtime, and the downloaded LibreHardwareMonitor and CoreCycler) live in the install folder. The installer locks that folder so only administrators can change it, even when a custom folder is chosen.
- **User-writable data** (`%APPDATA%\Headroom`) is treated as untrusted. Safety limits in `profile.json` are clamped to hard caps when loaded. Every GPU clock offset is checked against absolute limits at the NVML layer, wherever the request came from.
- **Claude Code (research):** only a `claude.exe` with a valid Authenticode signature from Anthropic, PBC is run, because the backend is elevated and the CLI lives in a user-writable folder. It runs in an empty temporary folder with only its WebSearch and WebFetch tools, no user settings, hooks or MCP servers, and without Headroom's API token or key in its environment, so web content can't make it run commands or change files.
- **API key (fallback):** stored encrypted with Windows DPAPI for the current user, and never returned by the API or logged.
- **Build:** the WebView2 SDK and all Python packages are pinned by SHA-256 (`installer/build.ps1`, `installer/requirements.txt`).
