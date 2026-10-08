@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

echo ======================================================================
echo  live_sub 설치 - 한 번만 하면 됩니다
echo ======================================================================
echo.

REM ---- 파이썬 찾기 ----------------------------------------------------
set "PY="
python3 -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=python3"
if not defined PY (
  python -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=py -3"
)

if not defined PY (
  echo [X] 파이썬 3.9 이상을 찾지 못했습니다.
  echo.
  echo     https://www.python.org/downloads/ 에서 설치하세요.
  echo     설치 첫 화면의 "Add python.exe to PATH" 를 꼭 체크해야 합니다.
  echo.
  pause
  exit /b 1
)
echo [OK] 파이썬: %PY%
echo.

REM ---- 1/4 핵심 ------------------------------------------------------
echo [1/4] 소리 캡처와 음성 인식 ...
%PY% -m pip install --quiet --upgrade pip
%PY% -m pip install --quiet numpy sounddevice soundcard faster-whisper
if errorlevel 1 goto failed

REM ---- 2/4 번역 ------------------------------------------------------
echo [2/4] 번역기 (로컬 - 제한 없음) ...
%PY% -m pip install --quiet "deep-translator>=1.11.4" transformers sentencepiece torch
if errorlevel 1 goto failed

REM ---- 3/4 GPU -------------------------------------------------------
echo [3/4] GPU 가속 (NVIDIA 없으면 자동으로 건너뜁니다) ...
%PY% -c "import ctranslate2,sys; sys.exit(0 if ctranslate2.get_cuda_device_count()>0 else 1)" >nul 2>&1
if errorlevel 1 (
  echo      NVIDIA GPU 가 없어 건너뜁니다. CPU 로 동작합니다.
) else (
  echo      용량이 큽니다. 몇 분 걸립니다 ...
  %PY% -m pip install --quiet nvidia-cublas-cu12 nvidia-cudnn-cu12
)

REM ---- 4/4 점검 ------------------------------------------------------
echo.
echo [4/4] 점검
echo.
%PY% check.py

echo.
echo ======================================================================
echo  설치 끝. 이제 run.bat 을 더블클릭하면 자막이 뜹니다.
echo  (처음 실행할 때 음성 인식 모델을 한 번 내려받느라 몇 분 걸립니다)
echo ======================================================================
pause
exit /b 0

:failed
echo.
echo [X] 설치 중 오류가 났습니다. 위 메시지를 확인하세요.
pause
exit /b 1
