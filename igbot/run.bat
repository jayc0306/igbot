@echo off
chcp 65001 >nul
cd /d %~dp0
call venv\Scripts\activate
echo 봇 실행 중... 이 창을 닫으면 봇이 꺼져요.
python bot.py
pause
