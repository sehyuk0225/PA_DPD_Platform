@echo off
cd /d %~dp0
set PYTHONUTF8=1
echo ============================================
echo  PA Bench Monitor Site2 v1.4.36_JSH (Bench v2.5.121) - EXE build
echo  (RF PA Analyzer graph window included: PySide6 + matplotlib)
echo ============================================
python --version
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.9+ ^(check "Add to PATH"^).
  pause
  exit /b 1
)
python -m pip install --upgrade pyinstaller openpyxl pyvisa "PySide6>=6.5" "matplotlib>=3.7" numpy
if errorlevel 1 (
  echo [ERROR] package install failed. Check proxy / pip settings.
  pause
  exit /b 1
)
set NAME=PA-Bench-Monitor-Site2_v1.4.36_JSH
python -m PyInstaller --noconfirm --clean --onedir --windowed --name "%NAME%" --icon assets\bench.ico ^
  --paths pa_analyzer\src ^
  --hidden-import analyzer_child --hidden-import psu_panel --hidden-import site2 --hidden-import pa_bench_monitor --collect-submodules pyvisa --copy-metadata pyvisa ^
  --hidden-import pa_dpd_tester --hidden-import dpd_engine --hidden-import dpd_core --hidden-import instruments --hidden-import demo_pa --hidden-import bench_link --hidden-import dpd_torch --hidden-import ext_psu ^
  --hidden-import matplotlib.backends.backend_qtagg ^
  --collect-submodules app --collect-submodules core --collect-submodules excel ^
  --collect-submodules measurement --collect-submodules plotting --collect-submodules simulation ^
  --collect-submodules ui --collect-submodules utils --collect-submodules sparam ^
  --add-data "pa_analyzer\data\example;data\example" ^
  --add-data "pa_analyzer\assets;assets" ^
  --add-data "assets\bench.ico;bench_assets" --add-data "assets\bench.png;bench_assets" ^
  --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module pandas ^
  pa_bench_monitor_site2.py
if errorlevel 1 (
  echo [ERROR] Build failed.
  pause
  exit /b 1
)
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\%NAME%' -DestinationPath 'dist\%NAME%-win64.zip'"
echo.
echo  DONE:  dist\%NAME%\%NAME%.exe
echo         dist\%NAME%-win64.zip  (give this zip to other PCs - unzip, run the exe inside)
echo.
pause
