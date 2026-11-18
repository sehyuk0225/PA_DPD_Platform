@echo off
cd /d %~dp0
REM First time only:  python -m pip install openpyxl PySide6 matplotlib numpy pyvisa
python pa_bench_monitor_site2.py
if errorlevel 1 pause
