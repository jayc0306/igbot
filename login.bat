@echo off
chcp 65001 >nul
cd /d %~dp0
call venv\Scripts\activate
echo 텔레그램 계정 로그인 (봇은 꺼둔 상태에서 실행하세요)
python login.py
pause
