"""
LLC Scanner — Launcher Diagnostics
Run this directly with Python to see what the launcher sees on the installed machine.
Writes a log to the desktop: LLC-Scanner-Debug.txt
"""

import os
import sys
import subprocess
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

LOG_PATH = Path.home() / "Desktop" / "LLC-Scanner-Debug.txt"
APP_DIR  = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent
VENV_DIR = APP_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
REQS = APP_DIR / "requirements.txt"
MAIN_PY = APP_DIR / "main.py"

lines = []

def log(msg):
    print(msg)
    lines.append(msg)

log("=== LLC Scanner Launcher Diagnostics ===")
log(f"sys.executable      : {sys.executable}")
log(f"sys.frozen          : {getattr(sys, 'frozen', False)}")
log(f"APP_DIR             : {APP_DIR}")
log(f"APP_DIR exists      : {APP_DIR.exists()}")
log(f"VENV_DIR            : {VENV_DIR}")
log(f"VENV_PYTHON         : {VENV_PYTHON}")
log(f"VENV_PYTHON exists  : {VENV_PYTHON.exists()}")
log(f"REQS exists         : {REQS.exists()}")
log(f"MAIN_PY exists      : {MAIN_PY.exists()}")
log("")

# Check what's in APP_DIR
log("Files in APP_DIR:")
try:
    for f in sorted(APP_DIR.iterdir()):
        log(f"  {f.name}{'/' if f.is_dir() else ''}")
except Exception as e:
    log(f"  ERROR listing: {e}")
log("")

# Check Python locations
log("Python search results:")
local_app = os.getenv("LOCALAPPDATA", "")
log(f"  LOCALAPPDATA = {local_app}")

for minor in range(11, 16):
    p = Path(local_app) / "Programs" / "Python" / f"Python3{minor}" / "python.exe"
    log(f"  {p} -> {'EXISTS' if p.exists() else 'not found'}")

for candidate in ("python", "python3", "py"):
    try:
        r = subprocess.run([candidate, "-c",
                            "import sys; v=sys.version_info; print(v.major,v.minor)"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            log(f"  PATH '{candidate}' -> version {r.stdout.strip()}")
        else:
            log(f"  PATH '{candidate}' -> failed (rc={r.returncode})")
    except FileNotFoundError:
        log(f"  PATH '{candidate}' -> not found")
    except Exception as e:
        log(f"  PATH '{candidate}' -> error: {e}")
log("")

# Check if .venv already exists (subsequent launch path)
if VENV_PYTHON.exists():
    log("VENV exists — attempting launch and capturing output:")
    log(f"  Command: {VENV_PYTHON} {MAIN_PY}")
    log(f"  cwd: {APP_DIR}")
    log("")

    # Try importing key modules via the venv python to find missing deps
    log("Checking key imports via venv python:")
    for mod in ("tkinter", "PIL", "cv2", "imagehash", "numpy", "tcgdexsdk", "torch", "timm", "faiss"):
        try:
            r = subprocess.run(
                [str(VENV_PYTHON), "-c", f"import {mod}; print('ok')"],
                capture_output=True, text=True, timeout=15, cwd=str(APP_DIR)
            )
            if r.returncode == 0:
                log(f"  import {mod:15s} -> OK")
            else:
                err = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "unknown error"
                log(f"  import {mod:15s} -> FAILED: {err}")
        except Exception as e:
            log(f"  import {mod:15s} -> ERROR: {e}")
    log("")

    # Try running main.py and capture stdout/stderr
    log("Running main.py (5 second timeout):")
    try:
        r = subprocess.run(
            [str(VENV_PYTHON), str(MAIN_PY)],
            capture_output=True, text=True, timeout=5, cwd=str(APP_DIR)
        )
        log(f"  returncode: {r.returncode}")
        if r.stdout.strip():
            log("  stdout:")
            for line in r.stdout.strip().splitlines():
                log(f"    {line}")
        if r.stderr.strip():
            log("  stderr:")
            for line in r.stderr.strip().splitlines():
                log(f"    {line}")
    except subprocess.TimeoutExpired:
        log("  (process still running after 5s — this is normal if GUI launched successfully)")
    except Exception as e:
        log(f"  ERROR: {e}")
else:
    log("VENV does not exist — first-run setup would run")

# Write log
LOG_PATH.write_text("\n".join(lines), encoding="utf-8")

# Show summary in a dialog
root = tk.Tk()
root.withdraw()
messagebox.showinfo(
    "LLC Scanner Diagnostics",
    f"Diagnostic log written to your Desktop:\n{LOG_PATH}\n\nOpen the file to see full results."
)
root.destroy()
