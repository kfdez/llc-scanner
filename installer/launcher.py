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
        self.title("LLC Scanner - First-Time Setup")
        self.resizable(False, False)
        self.configure(bg="#1a1a2e")

        # ── Icons (favicon + logo image) ──────────────────────────────────────
        # Assets are bundled into the PyInstaller _MEIPASS temp folder.
        _assets = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets"

        # Favicon / taskbar icon
        try:
            _ico = _assets / "logo.ico"
            if _ico.exists():
                self.iconbitmap(default=str(_ico))
        except Exception:
            pass

        # Logo image displayed above the title text.
        # Use tk.PhotoImage directly (supports PNG natively in Tk 8.6+)
        # so we don't need Pillow bundled into the launcher exe.
        self._logo_photo = None  # keep reference to prevent GC
        try:
            _png = _assets / "logo_white.png"
            if _png.exists():
                self._logo_photo = tk.PhotoImage(file=str(_png))
                # Scale down to ~64px using Tk's subsample (image is likely 512px+)
                w = self._logo_photo.width()
                factor = max(1, w // 64)
                self._logo_photo = self._logo_photo.subsample(factor, factor)
        except Exception:
            pass

        # Centre on screen — do after icon so geometry is correct
        self.update_idletasks()
        w, h = 460, 220
        x = (self.winfo_screenwidth()  - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

        # Prevent closing during install
        self.protocol("WM_DELETE_WINDOW", lambda: None)

        # Logo image (if loaded)
        if self._logo_photo:
            tk.Label(self, image=self._logo_photo,
                     bg="#1a1a2e").pack(pady=(18, 4))
        else:
            tk.Frame(self, height=18, bg="#1a1a2e").pack()  # spacing fallback

        tk.Label(
            self, text="LLC Scanner", bg="#1a1a2e", fg="#e0e0e0",
            font=("Helvetica", 16, "bold"),
        ).pack(pady=(0, 4))

        self._status = tk.StringVar(value="Installing dependencies...")
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
            msg = "..." + msg[-67:]
        self.after(0, lambda: self._detail.set(msg))

    def finish(self):
        """Stop progress bar and allow close."""
        self.after(0, self._bar.stop)
        self.after(0, lambda: self.protocol("WM_DELETE_WINDOW", self.destroy))


# ── Install logic ──────────────────────────────────────────────────────────────

def _check_python_version(cmd: str) -> bool:
    """Return True if the given python command is 3.11+."""
    try:
        result = subprocess.run(
            [cmd, "-c", "import sys; v=sys.version_info; print(v.major,v.minor)"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            major, minor = map(int, result.stdout.strip().split())
            return major == 3 and minor >= 11
    except Exception:
        pass
    return False


def _find_system_python() -> str | None:
    """Find a Python 3.11+ interpreter.

    Search order:
      1. Any Python 3.11+ already on the system PATH (respects user's own install)
      2. Per-user Python 3.11 installed by the LLC Scanner installer
         (%LocalAppData%\\Programs\\Python\\Python311\\python.exe)
         — only reached if the user had no existing Python 3.11+
    """
    # Priority 1: system PATH — covers Python 3.11, 3.12, 3.13, etc.
    # If the user already has a newer Python we should use it, not override it.
    for candidate in ("python", "python3", "py"):
        if _check_python_version(candidate):
            return candidate

    # Priority 2: fallback to the bundled Python 3.11 installed by the LLC Scanner
    # installer (InstallAllUsers=0, PrependPath=0 — so it won't be on PATH)
    if sys.platform == "win32":
        local_app = os.getenv("LOCALAPPDATA", "")
        bundled = Path(local_app) / "Programs" / "Python" / "Python311" / "python.exe"
        if bundled.exists() and _check_python_version(str(bundled)):
            return str(bundled)

    return None


def _show_error_and_close(window: SetupWindow, title: str, message: str):
    """Show a blocking error dialog on the main thread, then close the window."""
    from tkinter import messagebox
    def _do():
        window.withdraw()   # hide progress window while dialog is open
        messagebox.showerror(title, message)
        window.destroy()
    window.after(0, _do)


def _run_setup(window: SetupWindow):
    """Run in a background thread: create venv + pip install."""
    # Ensure all subprocesses run in UTF-8 mode
    os.environ["PYTHONUTF8"] = "1"
    try:
        python_cmd = _find_system_python()
        if python_cmd is None:
            _show_error_and_close(
                window,
                "Setup Failed - Python Not Found",
                "LLC Scanner could not locate Python 3.11.\n\n"
                "Please try uninstalling and reinstalling LLC Scanner.\n\n"
                "If the problem persists, install Python 3.11 manually from:\n"
                "https://www.python.org/downloads/\n"
                "(Use the default options when installing.)"
            )
            return

        # Step 1: create venv
        window.set_status("Creating virtual environment...")
        window.set_detail(str(VENV_DIR))
        result = subprocess.run(
            [python_cmd, "-m", "venv", str(VENV_DIR)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            _show_error_and_close(
                window,
                "Setup Failed - Venv Error",
                "Could not create the Python environment.\n\n"
                f"Error:\n{result.stderr.strip() or result.stdout.strip()}"
            )
            return

        # Step 2: pip install
        window.set_status("Installing dependencies (this may take a few minutes)...")
        window.set_detail("Downloading packages from PyPI...")

        pip_cmd = [
            str(VENV_PYTHON), "-m", "pip", "install",
            "--upgrade", "-r", str(REQS),
            "--no-warn-script-location",
        ]
        proc = subprocess.Popen(
            pip_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            encoding="utf-8", errors="replace",
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                window.set_detail(line)
        proc.wait()

        if proc.returncode != 0:
            _show_error_and_close(
                window,
                "Setup Failed - Install Error",
                "Failed to install dependencies.\n\n"
                "Please check your internet connection and try launching again.\n"
                "If the problem persists, contact support."
            )
            return

        window.set_status("Setup complete!")
        window.set_detail("Launch LLC Scanner from the Start Menu or desktop shortcut.")

    except Exception as exc:
        _show_error_and_close(
            window,
            "Setup Error",
            f"An unexpected error occurred during setup:\n\n{exc}"
        )
        return

    # Close automatically after a few seconds so the user can read the message.
    window.after(3000, window.destroy)


# ── App launcher ───────────────────────────────────────────────────────────────

def _launch_app():
    """Start main.py using the venv Python via pythonw.exe (no console window)."""
    pythonw = VENV_PYTHON.parent / "pythonw.exe"
    python_cmd = pythonw if pythonw.exists() else VENV_PYTHON

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    # Strip all PyInstaller-injected Tcl/Tk env vars from the child's environment.
    # When launcher.exe runs, PyInstaller sets TCL_LIBRARY / TK_LIBRARY / TCLLIBPATH
    # pointing at its own _MEI* temp folder. The child pythonw.exe inherits these
    # and tries to load Tcl from there instead of from C:\Python313, which causes:
    #   _tkinter.TclError: Can't find a usable init.tcl in the following directories:
    #       {C:\Users\...\Temp\_MEI348762\_tcl_data} ...
    # Removing them lets pythonw find its own Tcl installation normally.
    for _var in ("TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH",
                 "TCL_DATA", "_MEIPASS2", "_PYI_ONEFILE_PARENT"):
        env.pop(_var, None)

    subprocess.Popen(
        [str(python_cmd), "-X", "utf8", str(MAIN_PY)],
        cwd=str(APP_DIR),
        env=env,
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
