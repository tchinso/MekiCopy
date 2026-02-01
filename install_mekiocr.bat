@echo off
setlocal

where python >nul 2>&1
if errorlevel 1 (
  echo Python이 PATH에 없습니다. MS 스토어판 Python 3.13을 설치한 뒤 다시 실행하세요.
  exit /b 1
)

python -m pip install --upgrade pip
python -m pip install meikiocr mss pillow pyperclip

echo 설치가 완료되었습니다.
endlocal
