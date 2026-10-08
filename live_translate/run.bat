@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

REM 일본어 영상 -> 한국어 자막.
REM 소리는 "지금 Windows 가 쓰는 출력 장치" 를 자동으로 따라갑니다.
REM 특정 장치를 고정하려면:  run.bat --device G733
REM 더 빠르게:               run.bat --fast
REM 지연 측정:               run.bat --timing

set "PY="
python3 -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=python3"
if not defined PY (
  python -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
  echo 파이썬을 찾지 못했습니다. setup.bat 을 먼저 실행하세요.
  pause & exit /b 1
)

%PY% live_sub.py --src ja --dst ko --translator local --model medium %*

echo.
echo 종료되었습니다. 창을 닫으려면 아무 키나 누르세요.
pause >nul
