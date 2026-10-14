"""
LG4All Bidding Bot - Executable Build Script
===========================================
Automatically compiles lg4all_app.py into a standalone Windows Software (.exe)
using PyInstaller.
"""

import os
import sys
import shutil
import subprocess

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

def build_exe():
    print("==================================================")
    print("  LG4All Bidding Bot Executable Builder")
    print("==================================================")
    
    # 1. Install PyInstaller if missing
    try:
        import PyInstaller
        print("✓ PyInstaller is installed.")
    except ImportError:
        print("Installing PyInstaller via pip...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lg4all_app.py")
    dist_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")
    target_dist = os.path.join(dist_dir, "LG4All_Bidding_Bot")

    # Clean existing dist folder if possible
    if os.path.exists(target_dist):
        try:
            shutil.rmtree(target_dist, ignore_errors=True)
        except Exception:
            pass
    
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name=LG4All_Bidding_Bot",
        f"--distpath={dist_dir}",
        "--exclude-module=torch",
        "--exclude-module=bitsandbytes",
        "--exclude-module=scipy",
        "--exclude-module=matplotlib",
        "--exclude-module=pandas",
        script_path
    ]
    
    print("\nCompiling Python code into single executable...")
    print("Command:", " ".join(cmd))
    
    res = subprocess.run(cmd)
    if res.returncode == 0:
        print("\n==================================================")
        print("🎉 SUCCESS! Executable built successfully!")
        print(f"🚀 Executable File: {os.path.join(dist_dir, 'LG4All_Bidding_Bot.exe')}")
        print("==================================================")
    else:
        print("\n❌ Build failed. Please check logs.")

if __name__ == "__main__":
    build_exe()

