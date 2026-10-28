# External telemetry overlay prototype

## Single-file menu (recommended)

Download this repository using **Code → Download ZIP**, extract it, and open
PowerShell in the extracted folder. Use 64-bit Python 3.11 or newer:

```powershell
py -m pip install PySide6==6.10.2
py rust_overlay.py
```

`rust_overlay.py` contains its own menu, overlay, Windows process/module
enumeration and read-only memory reader. It does not import the other project
scripts, need a separate config file, or require a second terminal. PySide6 is
the only external package. Settings are saved through Qt's user settings.

1. Click **Start demo** to check the overlay and customize its appearance.
2. For live attempts, start Rust, then click **Find Rust process**.
3. Leave module selection at **Auto — check supplied layout** and click
   **Start read-only reader**. Auto discovers module load addresses and tests
   the supplied GOM layout only in modules large enough to contain that offset.
4. Read the status in the menu. Access denial or layout failure means no live
   ESP is available; this program cannot repair outdated offsets or bypass
   protected process access. There is no need to type a module address.
5. **Stop / clear** removes all overlay marks. **Exit** stops the worker, closes
   its process handle, and exits both windows.

Boxes, names, distance, health bars, snaplines, crosshair, maximum range, color,
font size, line width and overlay FPS update immediately from the menu. The
read loop runs at up to 20 Hz; rendering runs independently. Keep the menu
available on the taskbar; the overlay itself is click-through.

**This is not a verified working ESP for Rust build 26140533.** Supplied offsets
remain unverified. Automatic module checks eliminate manual base entry, but
they do not prove game-build compatibility. A readable camera or an empty
snapshot does not prove correct player data. No anti-cheat bypass is included.
Use a borderless game on the primary monitor at 1920×1080 and 100% scaling for
the initial attempt. Windows/live-game testing has not been performed here.

Six offscreen Linux checks cover demo/menu updates, synthetic entity traversal
and projection, missing-camera rejection, automatic layout rejection, simulated
access failure, and reader shutdown. They do not validate Windows API calls or
live Rust compatibility. Run them with:

```powershell
py -m unittest -v test_single_overlay
```

## Legacy separate scripts

This is a configurable external overlay with an experimental read-only Windows
memory adapter. **Compatibility with Rust build 26140533 is unverified.** It
does not bypass EAC or provide detection guarantees. The supplied layouts may
fail on the installed game; a working live Rust ESP has not been demonstrated.

## Windows 11

Install Python 3.11 or newer, then from this directory:

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python overlay.py --demo
```

The primary-screen overlay is transparent, always on top, and click-through.
Use windowed/borderless mode. Stop it with Ctrl+C in the launching terminal or
terminate its Python process in Task Manager. Windows behavior has not been
validated in this Linux development environment.

Edit `config.json` while running: changes reload every second. Configure box,
name, distance, health-bar, snapline and crosshair visibility, color, line width,
font size, maximum range and refresh rate. Invalid config retains the previous
settings. Health is interpreted on a 0–100 scale.

Without `--demo`, the overlay accepts UTF-8 JSON UDP packets on **127.0.0.1:27841**.
Coordinates are primary-screen logical pixels, origin top-left; `x,y` are the
box top-left. Producers must perform projection and DPI conversion themselves.
Packets are snapshots; stale snapshots disappear. There is no bundled producer
verified for current Rust. Example payload:

```json
{"entities":[{"x":400,"y":200,"w":80,"h":180,"name":"Example","distance":42,"health":75}]}
```

## Offset research

The public source https://github.com/GlebYoutuber/RustOffsets was inspected at
revision `f2193c9ef7806da201360f393270ad67b4482921`. Its README gives no compatible
game build. Its offsets are **not verified for current Facepunch Rust** and were
not copied into this project. A current build identifier, verified entity-list
layout, camera layout and access mechanism are required for live integration.

## Experimental supplied-layout reader

`memory_reader.py` uses Windows `OpenProcess` and `ReadProcessMemory` with read
rights only. It walks the attached tagged-object layout, finds camera tag 5 and
player tag 6, reads the supplied transforms, names and health, projects standing
boxes and sends snapshots to the overlay. It never writes game memory.

The attachment supplies GOM offset `0x125D5C0` without identifying its owning
module. **You must establish that module and its current load base first**;
ASLR changes bases between runs. Do not substitute an invented base address.
The inventory and active-item offsets also conflict between the supplied text
and attachment; this reader does not use those fields. Neither source establishes
compatibility with build 26140533.

Start the overlay without demo in one terminal:

```powershell
.\.venv\Scripts\python overlay.py
```

In another terminal, replace `PID` with the RustClient.exe process ID and
`VERIFIED_HEX_BASE` with the verified module base address, including `0x`:

```powershell
.\.venv\Scripts\python memory_reader.py --pid PID --module-base VERIFIED_HEX_BASE --width 1920 --height 1080
```

Use the primary monitor at 1920×1080, 100% Windows scaling, borderless mode, and
a game window covering that monitor. Position assumes Y-up world space; matrix
indexing follows the supplied transpose routine with screen Y flipped downward.
Boxes use an approximate 1.8-unit standing height, not bones. Team filtering,
visibility checks, crouch height and distinguishing the local player reliably
are not implemented. Players within one unit of the camera are omitted.

If Windows denies process access, the reader reports the error and exits; no
protection bypass is implemented. Invalid roots/cameras generate `READ FAILED`
messages and clear the overlay. Individual invalid/destroyed players are skipped.
An empty snapshot does not prove the offsets are correct. Report the exact
diagnostic and the source/build provenance of any corrected layouts.

Validation performed on Linux: synthetic pointer-chain traversal and projection,
behind-camera rejection and cyclic-list termination. Live Windows process access
and Rust rendering remain untested.
