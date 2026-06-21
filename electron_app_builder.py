import os, sys, subprocess

HERE    = os.path.dirname(os.path.abspath(__file__))
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.isfile(PYTHONW):
    PYTHONW = sys.executable
SCRIPT  = os.path.join(HERE, "electron.py")
ICON    = os.path.join(HERE, "icons", "movie.ico")
NAME    = "StreamVault"

try:
    import winshell
except ImportError:
    print("[setup] installing winshell + pywin32...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pywin32", "winshell"])
    scripts = os.path.join(os.path.dirname(sys.executable), "Scripts")
    post    = os.path.join(scripts, "pywin32_postinstall.py")
    if os.path.isfile(post):
        subprocess.check_call([sys.executable, post, "-install"], cwd=scripts)
    # restart using subprocess instead of execv to avoid space-in-path split
    result = subprocess.run([sys.executable] + sys.argv)
    sys.exit(result.returncode)

import winshell

def _make(dest):
    with winshell.shortcut(dest) as lnk:
        lnk.path              = PYTHONW
        lnk.arguments         = f'"{SCRIPT}"'
        lnk.working_directory = HERE
        lnk.description       = "StreamVault - Telegram media streaming"
        if os.path.isfile(ICON):
            lnk.icon_location = (ICON, 0)
    print(f"  OK {dest}")

if __name__ == "__main__":
    _make(os.path.join(winshell.desktop(), f"{NAME}.lnk"))
    _make(os.path.join(winshell.programs(), f"{NAME}.lnk"))
    print()
    print("Done! Right-click StreamVault in Start -> Pin to Start.")
    print("Python :", PYTHONW)
    print("Icon   :", ICON if os.path.isfile(ICON) else "not found - using default")