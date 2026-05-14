@echo off
title Trading Journal - Local Dev
cd /d "d:\Workflows\Business idea generator\trading-journal-prototype"
echo [*] Starting Trading Journal on http://localhost:8505
echo.
python -m streamlit run app.py --server.port=8505
pause
