@echo off
REM 일본어 영상 -> 한국어 자막. 더블클릭하면 바로 실행됩니다.
REM 다른 옵션을 주고 싶으면 이 파일을 드래그해서 인자를 덧붙이거나 아래 줄을 고치세요.
cd /d "%~dp0"
python live_sub.py --src ja --dst ko --translator local --model medium %*
echo.
echo 종료되었습니다. 창을 닫으려면 아무 키나 누르세요.
pause >nul
