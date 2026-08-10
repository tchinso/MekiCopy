@echo off
setlocal

where python >nul 2>&1
if errorlevel 1 (
  echo Python이 PATH에 없습니다. Python 3.12~3.14를 설치한 뒤 다시 실행하세요.
  exit /b 1
)

python -c "import sys, tkinter; raise SystemExit(0 if (3, 12) <= sys.version_info[:2] < (3, 15) else 1)"
if errorlevel 1 (
  echo Python 3.12~3.14와 Tcl/Tk가 필요합니다.
  exit /b 1
)

python -m pip install --upgrade pip==26.2.1
if errorlevel 1 exit /b 1
python -m pip install --upgrade --upgrade-strategy eager --requirement "%~dp0requirements-build.txt"
if errorlevel 1 exit /b 1
python -m pip install --force-reinstall --no-deps onnxruntime-gpu==1.28.0
if errorlevel 1 exit /b 1

echo 설치가 완료되었습니다.
endlocal & exit /b 0
