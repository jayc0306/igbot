"""환경변수 설정. .env 파일 또는 시스템 환경변수에서 읽음."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / "work"
FONT_DIR = BASE_DIR / "fonts"
WORK_DIR.mkdir(exist_ok=True)


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "y")


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


# --- Telegram 봇 ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# 숫자 id만 인정 (@아이디 같은 글자를 넣으면 무시하고 경고)
ALLOWED_USER_IDS = [int(x) for x in _list("ALLOWED_USER_IDS") if x.lstrip("-").isdigit()]
if len(ALLOWED_USER_IDS) != len(_list("ALLOWED_USER_IDS")):
    print("⚠️ ALLOWED_USER_IDS에는 @아이디가 아니라 숫자 id를 넣어야 해요. 봇에 /start를 보내면 숫자를 알려줘요.")
# 자동 수집 결과를 보낼 채팅 (비우면 ALLOWED_USER_IDS 첫 번째)
NOTIFY_CHAT_ID = int(os.getenv("NOTIFY_CHAT_ID") or (ALLOWED_USER_IDS[0] if ALLOWED_USER_IDS else 0))

# --- Claude ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
CONTEXT_SEARCH = _bool("CONTEXT_SEARCH", True)  # 원고 쓸 때 웹 검색으로 사건 배경 확인

# --- Instagram ---
IG_USER_ID = os.getenv("IG_USER_ID", "")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "")
IG_GRAPH_HOST = os.getenv("IG_GRAPH_HOST", "graph.instagram.com")
IG_API_VERSION = os.getenv("IG_API_VERSION", "v23.0")

# --- 미디어 호스팅 (Cloudflare R2 / S3 호환) ---
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL") or None
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_PUBLIC_BASE_URL = os.getenv("S3_PUBLIC_BASE_URL", "").rstrip("/")
DELETE_AFTER_PUBLISH = _bool("DELETE_AFTER_PUBLISH", True)

# --- 사진/영상 소스 ---
# 첨부 미디어가 없을 때 주제에 맞는 무료 스톡 사진/영상을 자동으로 찾아 씀
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")   # 우선 사용
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")     # 기존 키가 있으면 보조로 사용

# --- 텔레그램 채널 자동 수집 (내 텔레그램 계정으로 읽기) ---
TG_API_ID = int(os.getenv("TG_API_ID") or 0)
TG_API_HASH = os.getenv("TG_API_HASH", "")
SOURCE_CHANNELS = _list("SOURCE_CHANNELS")          # @채널아이디 또는 t.me 링크, 쉼표 구분
SCHEDULE_TIMES = _list("SCHEDULE_TIMES") or ["08:30", "18:30"]
TIMEZONE = os.getenv("TIMEZONE", "Asia/Seoul")
ISSUES_PER_RUN = int(os.getenv("ISSUES_PER_RUN", "2"))
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "12"))
# true: 채널 글에 붙은 사진/영상을 그대로 사용 (출처 표기). false: 스톡 사진만 사용
MEDIA_FROM_SOURCE = _bool("MEDIA_FROM_SOURCE", True)

# --- 동작 ---
# manual: 만든 파일을 텔레그램으로 받아서 직접 업로드 (인스타 API/R2 필요 없음)
# api: 봇이 인스타에 바로 게시 (IG_*, S3_* 설정 필요)
UPLOAD_MODE = os.getenv("UPLOAD_MODE", "manual").strip().lower()
AUTO_PUBLISH = _bool("AUTO_PUBLISH", False)

# --- 브랜드 / 톤 ---
BRAND_NAME = os.getenv("BRAND_NAME", "thecontext.kr")
BRAND_HANDLE = os.getenv("BRAND_HANDLE", "@thecontext.kr")
BRAND_DISPLAY = os.getenv("BRAND_DISPLAY", "더컨텍스트")             # 마지막 장 프사 아래 이름
PROFILE_IMAGE = os.getenv("PROFILE_IMAGE", str(BASE_DIR / "assets" / "profile.png"))
ACCOUNT_TOPIC = os.getenv("ACCOUNT_TOPIC", "국내외 이슈, 경제, 트렌드")
ACCOUNT_AUDIENCE = os.getenv("ACCOUNT_AUDIENCE", "2040")
FIXED_HASHTAGS = os.getenv("FIXED_HASHTAGS", "")
HASHTAG_LIMIT = int(os.getenv("HASHTAG_LIMIT", "5"))  # 해시태그 최대 개수 (고정 포함)

# 카드뉴스 마지막 장: 프사 + "OO 궁금한 친구에게 보내주세요" + 팔로우 버튼
OUTRO_ENABLED = _bool("OUTRO_ENABLED", True)
OUTRO_SUB = os.getenv("OUTRO_SUB", "팔로우하면 매일 화제 소식")
OUTRO_FOOT = os.getenv("OUTRO_FOOT", "저장하고 지인에게도 공유해주세요")

COLOR_ACCENT = os.getenv("COLOR_ACCENT", "#E5262B")     # 강조색 (빨강). 초록 예: #19E58C
COLOR_BG = os.getenv("COLOR_BG", "#000000")
COLOR_TEXT = os.getenv("COLOR_TEXT", "#FFFFFF")
COLOR_SUB = os.getenv("COLOR_SUB", "#9A9A9A")

# --- 릴스 ---
REEL_SUBLINE = os.getenv("REEL_SUBLINE", "▼ 자세한 내용은 캡션에 ▼")
# false(기본): 영상 위 자막 없이 원본 영상을 그대로 (상단 제목 + 하단 브랜드만)
# true: 장면별 자막 박스 + 내레이션
REEL_SUBTITLES = _bool("REEL_SUBTITLES", False)
REEL_MAX_SEC = float(os.getenv("REEL_MAX_SEC", "60"))      # 릴스 최대 길이 (초), 넘으면 잘라냄
KEEP_SOURCE_AUDIO = _bool("KEEP_SOURCE_AUDIO", True)       # 원본 영상 소리 유지
REEL_PHOTO_SEC = float(os.getenv("REEL_PHOTO_SEC", "3.5"))  # 사진만 있을 때 장당 길이
TTS_ENABLED = _bool("TTS_ENABLED", False)
TTS_VOICE = os.getenv("TTS_VOICE", "ko-KR-SunHiNeural")
TTS_RATE = os.getenv("TTS_RATE", "+12%")
BGM_PATH = os.getenv("BGM_PATH", "")
BGM_VOLUME = float(os.getenv("BGM_VOLUME", "0.15"))
