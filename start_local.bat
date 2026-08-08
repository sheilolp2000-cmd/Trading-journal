@echo off
title Trading Journal - Local Dev
cd /d "%~dp0"
echo [*] Starting Trading Journal on http://localhost:8505
echo.
python -m streamlit run app.py --server.port=8505
pause
