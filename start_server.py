#!/usr/bin/env python
import subprocess
import time
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

print("[*] Starting Trading Journal App...")
print("[*] URL: http://localhost:8501")
print("[*] Window can be closed, app continues running...\n")

try:
    subprocess.Popen([
        sys.executable, "-m", "streamlit", "run", "app.py",
        "--logger.level=error",
        "--client.showErrorDetails=false"
    ])
    print("[OK] App running on http://localhost:8501")
    time.sleep(2)
except Exception as e:
    print(f"[ERROR] {e}")
    input("Press Enter to close...")
