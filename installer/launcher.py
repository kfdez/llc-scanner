"""
LLC Scanner — Launcher / First-Run Bootstrapper

This script is compiled into a small launcher.exe by PyInstaller.
On first launch it installs dependencies into a venv, then starts the app.
On subsequent launches it just starts the app directly.

The compiled launcher.exe is placed at the root of the install directory
by the Inno Setup installer. It expects:
    {install_dir}/
        launcher.exe        ← this script, compiled
        main.py
        requirements.txt
        config.py
        cards/  db/  ebay/  gui/  identifier/
"""

import os
import sys
import subprocess
import tkinter as tk
from tkinter import ttk
from pathlib import Path
import threading


# ── Paths ──────────────────────────────────────────────────────────────────────

# When compiled by PyInstaller --onefile, sys.executable is launcher.exe itself.
# The app files are installed alongside it.
APP_DIR  = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent
VENV_DIR = APP_DIR / ".venv"
MAIN_PY  = APP_DIR / "main.py"
REQS     = APP_DIR / "requirements.txt"

# Python inside the venv created during first-run install
if sys.platform == "win32":
    VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
else:
    VENV_PYTHON = VENV_DIR / "bin" / "python"


# ── First-run detection ────────────────────────────────────────────────────────

def _needs_setup() -> bool:
    """Return True if the venv hasn't been created yet."""
    return not VENV_PYTHON.exists()


# ── Progress window ────────────────────────────────────────────────────────────

class SetupWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LLC Scanner — First-Time Setup")
        self.resizable(False, False)
        self.configure(bg="#1a1a2e")

        # Centre on screen
        self.update_idletasks()
        w, h = 460, 180
        x = (self.winfo_screenwidth()  - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

        # Prevent closing during install
        self.protocol("WM_DELETE_WINDOW", lambda: None)

        tk.Label(
            self, text="LLC Scanner", bg="#1a1a2e", fg="#e0e0e0",
            font=("Helvetica", 16, "bold"),
        ).pack(pady=(24, 4))

        self._status = tk.StringVar(value="Installing dependencies…")
        tk.Label(
            self, textvariable=self._status, bg="#1a1a2e", fg="#a0a0b0",
            font=("Helvetica", 10),
        ).pack(pady=(0, 12))

        self._bar = ttk.Progressbar(self, mode="indeterminate", length=380)
        self._bar.pack(pady=(0, 8))
        self._bar.start(12)

        self._detail = tk.StringVar(value="")
        tk.Label(
            self, textvariable=self._detail, bg="#1a1a2e", fg="#606080",
            font=("Helvetica", 8),
        ).pack()

    def set_status(self, msg: str):
        self.after(0, lambda: self._status.set(msg))

    def set_detail(self, msg: str):
        # Truncate long lines so the window doesn't resize
        if len(msg) > 70:
            msg = "…" + msg[-67:]
        self.after(0, lambda: self._detail.set(msg))

    def finish(self):
        """Stop progress bar and allow close."""
        self.after(0, self._bar.stop)
        self.after(0, lambda: self.protocol("WM_DELETE_WINDOW", self.destroy))


# ── Install logic ──────────────────────────────────────────────────────────────

def _find_system_python() -> str | None:
    """Find a Python 3.11+ interpreter on the system PATH."""
    for candidate in ("python", "python3", "py"):
        try:
            result = subprocess.run(
                [candidate, "-c",
                 "import sys; v=sys.version_info; print(v.major,v.minor)"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                major, minor = map(int, result.stdout.strip().split())
                if major == 3 and minor >= 11:
                    return candidate
        except Exception:
            continue
    return None


def _run_setup(window: SetupWindow):
    """Run in a background thread: create venv + pip install."""
    try:
        python_cmd = _find_system_python()
        if python_cmd is None:
            window.set_status("Python 3.11+ not found!")
            window.set_detail(
                "Please install Python 3.11 from python.org, then re-launch."
            )
            window.finish()
            return

        # Step 1: create venv
        window.set_status("Creating virtual environment…")
        window.set_detail(str(VENV_DIR))
        subprocess.run(
            [python_cmd, "-m", "venv", str(VENV_DIR)],
            check=True, capture_output=True,
        )

        # Step 2: pip install
        window.set_status("Installing dependencies (this may take a few minutes)…")
        window.set_detail("Downloading packages from PyPI…")

        pip_cmd = [
            str(VENV_PYTHON), "-m", "pip", "install",
            "--upgrade", "-r", str(REQS),
            "--no-warn-script-location",
        ]
        proc = subprocess.Popen(
            pip_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                window.set_detail(line)
        proc.wait()

        if proc.returncode != 0:
            window.set_status("Dependency install failed.")
            window.set_detail("Check your internet connection and try again.")
            window.finish()
            return

        window.set_status("Setup complete! Launching LLC Scanner…")
        window.set_detail("")

    except Exception as exc:
        window.set_status("Setup error.")
        window.set_detail(str(exc))
        window.finish()
        return

    # Small delay so user can read the "complete" message, then close window
    window.after(1200, _launch_app_and_close, window)


def _launch_app_and_close(window: SetupWindow):
    """Launch the real app, then close the setup window."""
    _launch_app()
    window.after(200, window.destroy)


# ── App launcher ───────────────────────────────────────────────────────────────

def _launch_app():
    """Start main.py using the venv Python (detached, no console window)."""
    kwargs = {}
    if sys.platform == "win32":
        # DETACHED_PROCESS prevents a console window from appearing
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS

    subprocess.Popen(
        [str(VENV_PYTHON), str(MAIN_PY)],
        cwd=str(APP_DIR),
        **kwargs,
    )


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    if _needs_setup():
        window = SetupWindow()
        thread = threading.Thread(target=_run_setup, args=(window,), daemon=True)
        thread.start()
        window.mainloop()
    else:
        _launch_app()


if __name__ == "__main__":
    main()
