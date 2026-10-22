FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# data/ 에 텔레그램 로그인 세션과 수집 기록이 저장됨 → 볼륨으로 유지
VOLUME ["/app/data"]
CMD ["python", "bot.py"]
