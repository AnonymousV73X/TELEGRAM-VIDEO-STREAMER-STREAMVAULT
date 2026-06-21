import os, sys, subprocess

HERE    = os.path.dirname(os.path.abspath(__file__))
# .pyw is natively windowless — use python.exe so it can find the interpreter
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
PYTHON  = PYTHONW if os.path.isfile(PYTHONW) else sys.executable
SCRIPT  = os.path.join(HERE, "launcher.pyw")
ICON    = os.path.join(HERE, "icons", "movie.ico")
NAME    = "STREAM 2.0"

try:
    import winshell
except ImportError:
    print("[setup] installing winshell + pywin32...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pywin32", "winshell"])
    scripts = os.path.join(os.path.dirname(sys.executable), "Scripts")
    post    = os.path.join(scripts, "pywin32_postinstall.py")
    if os.path.isfile(post):
        subprocess.check_call([sys.executable, post, "-install"], cwd=scripts)
    result = subprocess.run([sys.executable] + sys.argv)
    sys.exit(result.returncode)

import winshell

def _make(dest):
    with winshell.shortcut(dest) as lnk:
        lnk.path              = PYTHON
        lnk.arguments         = f'"{SCRIPT}"'
        lnk.working_directory = HERE
        lnk.description       = "StreamVault — pywebview edition"
        if os.path.isfile(ICON):
            lnk.icon_location = (ICON, 0)
    print(f"  OK {dest}")

if __name__ == "__main__":
    _make(os.path.join(winshell.desktop(), f"{NAME}.lnk"))
    _make(os.path.join(winshell.programs(), f"{NAME}.lnk"))
    print()
    print("Done! Right-click in Start -> Pin to Start.")
    print("Python :", PYTHON)
    print("Script :", SCRIPT)
    print("Icon   :", ICON if os.path.isfile(ICON) else "not found - using default")