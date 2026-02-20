"""
LLC Scanner — Installer Build Script
=====================================
Automates the full pipeline:
  1. Converts logo PNG -> ICO (installer icon)
  2. Compiles launcher.py -> launcher.exe via PyInstaller
  3. Stages app source files into installer/dist/app/
  4. Runs Inno Setup compiler (iscc) -> LLC-Scanner-Setup.exe

Prerequisites (install once):
  pip install pyinstaller pillow
  Download Inno Setup 6 from https://jrsoftware.org/isdl.php

Usage:
  python installer/build_installer.py

The finished installer will be at:
  installer/dist/LLC-Scanner-Setup.exe
"""

import os
import sys
import stat
import shutil
import subprocess
import winreg
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = Path(__file__).parent                   # installer/
PROJECT_DIR = SCRIPT_DIR.parent                       # project root
DIST_DIR    = SCRIPT_DIR / "dist"
APP_STAGE   = DIST_DIR / "app"                        # staged source files
LAUNCHER_PY = SCRIPT_DIR / "launcher.py"
ICON_PNG    = PROJECT_DIR / "gui" / "assets" / "logo_white.png"
ICON_ICO    = PROJECT_DIR / "gui" / "assets" / "logo.ico"
REDIST_DIR  = SCRIPT_DIR / "redist"

# Files/dirs to include from the project root into the staged app folder
APP_FILES = [
    "main.py",
    "config.py",
    "requirements.txt",
]
APP_DIRS = [
    "cards",
    "db",
    "ebay",
    "gui",
    "identifier",
]

# Inno Setup compiler — fallback hard-coded paths (registry lookup is preferred)
ISCC_PATHS = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\iscc.exe"),
    Path(r"C:\Program Files\Inno Setup 6\iscc.exe"),
    Path(r"C:\Program Files (x86)\Inno Setup 5\iscc.exe"),
    Path(r"C:\Program Files\Inno Setup 5\iscc.exe"),
]


def _find_iscc() -> Path | None:
    """Locate iscc.exe via registry, PATH, or known install paths."""
    # 1. Check Windows registry (most reliable)
    reg_keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
    ]
    for hive, key_path in reg_keys:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                install_dir, _ = winreg.QueryValueEx(key, "InstallLocation")
                candidate = Path(install_dir) / "iscc.exe"
                if candidate.exists():
                    return candidate
        except OSError:
            continue

    # 2. Check PATH
    iscc_in_path = shutil.which("iscc")
    if iscc_in_path:
        return Path(iscc_in_path)

    # 3. Fall back to hard-coded locations
    for path in ISCC_PATHS:
        if path.exists():
            return path

    return None


# ── Step 1: PNG -> ICO ─────────────────────────────────────────────────────────

def build_icon():
    print("[1/4] Converting logo to .ico ...")
    try:
        from PIL import Image
    except ImportError:
        print("  Pillow not installed — skipping icon conversion.")
        print("  Run: pip install pillow")
        return

    if not ICON_PNG.exists():
        print(f"  Warning: {ICON_PNG} not found — skipping icon.")
        return

    img = Image.open(ICON_PNG).convert("RGBA")
    # Generate multiple sizes for the .ico multi-resolution format
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ICON_ICO, format="ICO", sizes=sizes)
    print(f"  -> {ICON_ICO}")


# ── Step 2: Compile launcher.exe ──────────────────────────────────────────────

def build_launcher():
    print("[2/4] Compiling launcher.exe via PyInstaller ...")

    icon_arg = str(ICON_ICO) if ICON_ICO.exists() else "NONE"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",           # no console window
        "--name", "launcher",
        "--distpath", str(SCRIPT_DIR),   # put launcher.exe in installer/
        "--workpath", str(DIST_DIR / "pyinstaller_build"),
        "--specpath", str(DIST_DIR),
        "--icon", icon_arg,
        str(LAUNCHER_PY),
    ]

    result = subprocess.run(cmd, cwd=str(PROJECT_DIR))
    if result.returncode != 0:
        print("  ERROR: PyInstaller failed. See output above.")
        sys.exit(1)

    launcher_exe = SCRIPT_DIR / "launcher.exe"
    if not launcher_exe.exists():
        print("  ERROR: launcher.exe not found after PyInstaller build.")
        sys.exit(1)

    print(f"  -> {launcher_exe}  ({launcher_exe.stat().st_size // 1024} KB)")


# ── Step 3: Stage app files ───────────────────────────────────────────────────

def stage_app():
    print("[3/4] Staging app source files ...")

    if APP_STAGE.exists():
        def _force_remove(func, path, exc_info):
            # Clear read-only flag and retry (handles OneDrive-locked files)
            os.chmod(path, stat.S_IWRITE)
            func(path)
        shutil.rmtree(APP_STAGE, onerror=_force_remove)
    APP_STAGE.mkdir(parents=True)

    for fname in APP_FILES:
        src = PROJECT_DIR / fname
        if src.exists():
            shutil.copy2(src, APP_STAGE / fname)
            print(f"  + {fname}")
        else:
            print(f"  WARNING: {fname} not found, skipping.")

    for dname in APP_DIRS:
        src = PROJECT_DIR / dname
        if src.exists():
            dst = APP_STAGE / dname
            shutil.copytree(
                src, dst,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            print(f"  + {dname}/")
        else:
            print(f"  WARNING: {dname}/ not found, skipping.")

    print(f"  -> Staged to {APP_STAGE}")


# ── Step 4: Run Inno Setup ────────────────────────────────────────────────────

def build_setup():
    print("[4/4] Building installer with Inno Setup ...")

    # Check redist folder has Python installer
    redist_files = list(REDIST_DIR.glob("python-3.11*.exe")) if REDIST_DIR.exists() else []
    if not redist_files:
        print()
        print("  !! Python installer not found in installer/redist/")
        print("  !! Download python-3.11.9-amd64.exe from:")
        print("  !!   https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe")
        print("  !! Place it in:  installer/redist/python-3.11.9-amd64.exe")
        print()
        print("  Skipping Inno Setup — all other files are ready.")
        print("  Re-run this script once python-3.11.9-amd64.exe is in place.")
        return

    iscc = _find_iscc()

    if iscc is None:
        print()
        print("  !! Inno Setup not found. Install from:")
        print("  !!   https://jrsoftware.org/isdl.php")
        print()
        print("  Skipping Inno Setup — all other files are ready.")
        print("  Re-run this script once Inno Setup is installed.")
        return

    print(f"  Inno Setup found: {iscc}")
    iss_file = SCRIPT_DIR / "installer.iss"
    result = subprocess.run([str(iscc), str(iss_file)], cwd=str(SCRIPT_DIR))
    if result.returncode != 0:
        print("  ERROR: Inno Setup failed. See output above.")
        sys.exit(1)

    output = SCRIPT_DIR / "dist" / "LLC-Scanner-Setup.exe"
    if output.exists():
        size_mb = output.stat().st_size / (1024 * 1024)
        print(f"  -> {output}  ({size_mb:.1f} MB)")
    else:
        print("  Done (output path unknown — check installer/dist/).")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  LLC Scanner — Build Installer")
    print("=" * 60)
    print()

    # Ensure we're running from the project root or installer dir
    if not (PROJECT_DIR / "main.py").exists():
        print("ERROR: main.py not found. Run this script from the project root:")
        print("  python installer/build_installer.py")
        sys.exit(1)

    build_icon()
    print()
    build_launcher()
    print()
    stage_app()
    print()
    build_setup()
    print()
    print("=" * 60)
    print("  Build complete.")
    print("  Installer: installer/dist/LLC-Scanner-Setup.exe")
    print("=" * 60)


if __name__ == "__main__":
    main()
