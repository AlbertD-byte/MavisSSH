# MavisSSH 0.2.0 — First Public Release

> A cross-platform desktop SSH client: an xterm-compatible terminal, SFTP, port
> forwarding, resource monitoring and lrzsz transfer. Written in Python + PyQt6,
> with host-key verification that **actually works**.

**Release date**: 2026-11-10
**License**: GPL-3.0-only
**Repository**: https://github.com/AlbertD-byte/MavisSSH

---

## What it is

MavisSSH is a desktop SSH client written in Python and PyQt6. It covers the
whole day-to-day ops chain — from "log in and run a command" to "move a file,
open a tunnel, watch resource usage".

It is not a Terminal clone, and it does not try to be a cloud orchestration
layer. It exists to answer one specific question: **when you do ops work on
Windows, do you need WSL, or a closed-source client?** Usually, you do not.

**There is a second reason for this project: security.** A great many SSH
clients accept any host key by default. MavisSSH makes host-key verification a
real workflow — show the SHA-256 fingerprint, trust once, trust and remember,
manage the trusted list — instead of a checkbox that ships ticked.

---

## Highlights

### Added

- **Host-key management**: SHA-256 fingerprint confirmation, "trust once" and
  "trust and remember", plus add/remove/import for the trusted-host list
  (`sshclient/remote/hostkeys.py`)
- **Session groups**: pick from a list or type your own, collapsible group
  sections, tab titles rendered as `group-session`
- **Import connections from other clients**: OpenSSH config / PuTTY / WinSCP —
  **passwords are never imported**
- **Command snippet library**: create, edit and delete; a built-in read-only
  command set; self-check for dangerous commands (including wrapper words and
  interpreter payloads)
- **Tab context menu**: close current / close others / close to the right
- **Quick-connect cards on the welcome page**: up to 5, one click to connect
- **Windows installer** (NSIS): optional install directory, optional desktop
  shortcut, uninstallable from Control Panel, Chinese + English, GPL-3.0 full
  text on the license page
- **Tunnel exposure warning**: new `is_exposed_address()` — a non-loopback
  listen address now requires explicit confirmation

### Security

Eight security issues were fixed in this release. The first two are
**man-in-the-middle** class:

| Issue | Impact | Resolution |
|---|---|---|
| **Host keys accepted unconditionally** | The old code used `AutoAddPolicy` and never loaded the system trust store, so **any man-in-the-middle could impersonate the server** | Explicit verification plus a fingerprint-confirmation flow |
| **lrzsz remote command injection** | Paths were interpolated without quoting, allowing command injection on the remote host | Every path is quoted individually; `~` is expanded by the remote `${HOME}`. **Both injection points are closed** — `conn.exec` and the interactive shell's `sendall` |
| **`proxy_command` local command execution** | `Popen(shell=True)` gave an arbitrary local command execution surface | **The feature was removed** |
| **Weak algorithms not disabled** | SHA-1 and other weak options remained negotiable under the default algorithm preferences | `disabled_algorithms` is now set explicitly, keeping the one strong option |
| **No tunnel exposure warning** | A tunnel bound to `0.0.0.0` was reachable from the whole network, silently | Non-loopback listen addresses require confirmation |
| **Authentication retries did not converge** | Repeated retries could be used to exhaust server-side resources | Retry count is now bounded |
| **Sensitive data leaked into logs** | Usable credential fragments were written to the log | Credential fields are redacted |
| **Dependency floor too loose** | `paramiko>=3.4` leaves vulnerable 4.x installs in existing environments | Tightened to `paramiko>=5.0,<6` |

> Full details, supported versions and the reporting process:
> [SECURITY.md](https://github.com/AlbertD-byte/MavisSSH/blob/main/SECURITY.md).

### Fixed

- **Crash on real machines**: a worker thread woke up and posted to an already
  destroyed widget → `RuntimeError`. `sshclient/ui/thread_post.py` now performs
  the liveness check in one place
- The connection worker could hang for 120 seconds when the main window closed
- **"First trust always fails"**: paramiko's `save_host_keys()` re-reads the
  file first, and on the very first trust the file does not exist yet →
  `FileNotFoundError` → handshake failure. **Offline criteria cannot catch
  this** — it was found by live testing
- The primary button in dialogs lost its solid fill, leaving only an outline
  that looked like "selected by default"
- Editing an existing session showed "Connect" instead of "Save"
- The tab close button was covered by the scroll buttons when the tab bar
  overflowed
- Scrolling back through terminal history froze a whole frame (6 s → 60 ms)
- Dragging the left or top edge did not move the window with the cursor
- The tab close button attached to the wrong tab, or vanished in narrow bars
- "Import from other clients" crashed on the first click

### Accessibility

This release also fixes three **real WCAG AA failures** — and, more importantly,
the fact that **the accessibility criteria had never been running at all**.

- **Dark-theme `danger` failed AA**: the old `#f35555` reached only **4.428**
  against `bg_hover_row` — the background of the "hot disk" row in the monitor
  panel while the mouse hovers it. Lifted to `#f65958` it is **4.603**, with a
  ΔE00 of only **1.035** (JND ≈ 2.3) — indistinguishable from the original by eye
- **The monitor panel's percent label sat on the status-colour fill**: only
  **2.163 / 2.150 / 2.865** in the dark theme — all three status colours failed
  AA. The label now sits on a capsule in the track colour, so it always rests
  on a background of known contrast
- **The title-bar and tab close glyphs borrowed `text_on_accent`** — that is
  derived from the accent, so using it over `danger` is a cross-family borrow
  (3.237 in the light theme). It is now derived from the fill itself
- **`tests/_uia11y.py` was not in the gate**: it is absent from the `TESTS`
  tuple in `_runall.py`, so CI never ran it either. **A gate that does not run
  is not a gate**
- **The criteria table was measuring the wrong pair**: the previous
  `danger/bg_dialog` (text tier) measured two backgrounds that never touch —
  there is no danger *text* in dialogs at all. Every row now corresponds to a
  real use site, with the graphic tier and the text tier kept apart

### Documentation and engineering

- **58 offline criteria suites** wired into a single gate, `_runall.py`, each
  in its own process with its own 180-second deadline
- **GitHub Actions CI**: runs the gate plus `ruff` on a Windows runner and
  uploads logs on failure. The config ships as a template at
  [`docs/ci.yml`](https://github.com/AlbertD-byte/MavisSSH/blob/main/docs/ci.yml);
  copy it to `.github/workflows/ci.yml` to enable it
- **No third-party test framework**: every criterion is a standalone executable
  Python script, so it can be re-run on a machine with no dev environment
- Added `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `CHANGELOG.md`
  and Issue / PR templates

---

## Installation

### Windows (installer)

Download `MavisSSH-0.2.0-Setup.exe` from
[Releases](https://github.com/AlbertD-byte/MavisSSH/releases) and run it.

### From source

Requires **Python 3.10+**. Always use the project-local virtual environment —
the application should not depend on whichever Python happens to be on `PATH`.

```console
git clone https://github.com/AlbertD-byte/MavisSSH.git
cd MavisSSH
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py
```

Use `.venv/bin/python` on macOS / Linux.

---

## Known limitations

To be straightforward about what has **not** been fully verified on real
machines at the time of this release:

- **macOS and Linux have not been verified on real hardware.** The cross-platform
  code paths are handled (config directories, CJK fallback fonts, where `rz`/`sz`
  come from), and `tests/_portable.py` guards them specifically — but the
  project has not yet been run end-to-end on those systems. Windows is the only
  fully verified platform.
- **Screen-reader support is limited.** The UI is custom-drawn and semantic
  annotations are not complete.
- **lrzsz transfer requires `lrzsz` installed on the remote host** (`sz` and
  `rz` on `PATH`). When it is missing, the feature is explicitly skipped
  rather than silently reported as successful.
- **The SFTP panel's file-list buttons are not covered by offline criteria** —
  they need a real `SFTPClient` to be constructed, and are covered by
  `tests/_live.py` against a real server.
- **The gate has only been exercised on Windows.** Some criteria depend on font
  metrics, and those numbers do not agree across platforms.

The full record of design trade-offs, pitfalls and unverified items lives in
[docs/ARCHITECTURE.md](https://github.com/AlbertD-byte/MavisSSH/blob/main/docs/ARCHITECTURE.md).

---

## Contributing

Issues and pull requests are welcome.

- **Bug reports**: please use the
  [bug template](https://github.com/AlbertD-byte/MavisSSH/blob/main/.github/ISSUE_TEMPLATE/bug_report.yml)
  and include reproduction steps plus the relevant excerpt of
  `logs/mavisssh.log` — **check the log for sensitive information first**
- **Pull requests**: see
  [CONTRIBUTING.md](https://github.com/AlbertD-byte/MavisSSH/blob/main/CONTRIBUTING.md),
  which documents **how to run the gate** and **how criteria should be written**
- **Discussion**: please follow the
  [Code of Conduct](https://github.com/AlbertD-byte/MavisSSH/blob/main/CODE_OF_CONDUCT.md)

### Developing with MiniMax Code

The criteria, architecture documentation and test report in this repository are
organised around the working conventions of **MiniMax Code (MCode)**, so anyone
new to the project can hand it straight to MCode. See the
[“Developing with MiniMax Code”](https://github.com/AlbertD-byte/MavisSSH/blob/main/README.md#使用-minimax-code-进行开发)
section of the README.

---

## License

**GNU General Public License v3.0 only (GPL-3.0-only).**

MIT and Apache-2.0 were not used because the distributed package carries
third-party binaries — `bin/rz.exe`, `bin/sz.exe` and `msys-2.0.dll` — which
are not part of paramiko but ship inside the bundle. Section 6(d) of GPL-3.0
allows a written offer for the corresponding source, and the GitHub Release
source tarball satisfies that requirement. `THIRD_PARTY_NOTICES.md` records all
third-party attributions, and `LICENSES/` holds the six upstream license texts.

**A note for users and redistributors**: GPL-3.0 is a copyleft licence. If you
modify this project and **distribute** your version (free or paid), you must
publish your modifications under GPL-3.0 and preserve the copyright notice and
licence of the original.

---

## Verification

SHA-256 checksums for the installer and the source tarballs are published with
the corresponding release assets:

```console
Get-FileHash .\MavisSSH-0.2.0-Setup.exe -Algorithm SHA256
```

**Maintainer** · 2026-11-10
