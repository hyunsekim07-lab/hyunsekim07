@echo off
REM Windows: 스피커로 나가는 소리를 자동으로 잡아 한국어 자막을 띄웁니다.
cd /d "%~dp0"
python live_sub.py --src ja --dst ko %*
pause
