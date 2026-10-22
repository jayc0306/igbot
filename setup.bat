@echo off
chcp 65001 >nul
cd /d %~dp0
echo [1/3] 가상환경 만드는 중...
if not exist venv python -m venv venv
call venv\Scripts\activate
echo [2/3] 패키지 설치 중... (몇 분 걸려요)
python -m pip install --upgrade pip
pip install -r requirements.txt
echo [3/3] 설정 파일 준비
if not exist .env copy .env.example .env
echo.
echo 설치 끝! 이제 .env 파일을 채우세요. (메모장이 열립니다)
notepad .env
pause
