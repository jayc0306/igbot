"""사진/영상 소스: 첨부 미디어, 텔레그램 채널 미디어, Pexels 스톡."""
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

import config
from ffbin import FFMPEG, duration

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class Media:
    path: Path
    kind: str          # "photo" | "video"
    credit: str = ""   # 이미지에 작게 표기할 출처
    origin: str = "source"  # source(보낸 소스) | article(기사 사진) | stock(스톡)


def detect_kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in PHOTO_EXT:
        return "photo"
    return None


def video_duration(path: Path) -> float:
    return duration(path)


def video_frame(video: Path, out: Path, t: float = 1.0) -> Path:
    """카드뉴스용: 영상에서 한 장면을 사진으로 추출."""
    dur = video_duration(video)
    t = min(t, max(0.0, dur / 3))
    subprocess.run([FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                    "-q:v", "2", str(out)], capture_output=True)
    return out


def as_photo(m: Media, out_dir: Path) -> Media:
    if m.kind == "photo":
        return m
    out = out_dir / f"{m.path.stem}_frame.jpg"
    video_frame(m.path, out)
    return Media(out, "photo", m.credit, m.origin)


# ---------- 스톡: Pixabay (우선) / Pexels ----------

def _pixabay(url: str, params: dict) -> dict | None:
    if not config.PIXABAY_API_KEY:
        return None
    try:
        r = httpx.get(url, params={"key": config.PIXABAY_API_KEY, "safesearch": "true", **params}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[Pixabay 실패] {e}")
        return None


def pixabay_photo(query: str, out_dir: Path, name: str, orientation: str = "portrait") -> Media | None:
    if not query:
        return None
    data = _pixabay("https://pixabay.com/api/", {
        "q": query[:100], "image_type": "photo", "per_page": 5,
        "orientation": "vertical" if orientation == "portrait" else "horizontal"})
    if not data or not data.get("hits"):
        return None
    h = data["hits"][0]
    out = _download(h["largeImageURL"], out_dir / f"{name}.jpg")
    return Media(out, "photo", f"Photo: {h.get('user', '')} / Pixabay", "stock") if out else None


def pixabay_video(query: str, out_dir: Path, name: str) -> Media | None:
    if not query:
        return None
    data = _pixabay("https://pixabay.com/api/videos/", {"q": query[:100], "per_page": 5})
    if not data or not data.get("hits"):
        return None
    for h in data["hits"]:
        vids = h.get("videos", {})
        for size in ("large", "medium"):
            url = (vids.get(size) or {}).get("url")
            if url:
                out = _download(url, out_dir / f"{name}.mp4")
                if out:
                    return Media(out, "video", f"Video: {h.get('user', '')} / Pixabay", "stock")
    return None


def stock_photo(query: str, out_dir: Path, name: str, orientation: str = "portrait") -> Media | None:
    return pixabay_photo(query, out_dir, name, orientation) or pexels_photo(query, out_dir, name, orientation)


def stock_video(query: str, out_dir: Path, name: str) -> Media | None:
    return pixabay_video(query, out_dir, name) or pexels_video(query, out_dir, name)


# ---------- Pexels (기존 키가 있는 경우) ----------

def _pexels_get(url: str, params: dict) -> dict | None:
    if not config.PEXELS_API_KEY:
        return None
    try:
        r = httpx.get(url, params=params, headers={"Authorization": config.PEXELS_API_KEY}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[Pexels 실패] {e}")
        return None


def _download(url: str, out: Path) -> Path | None:
    try:
        with httpx.stream("GET", url, timeout=60, follow_redirects=True) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return out
    except Exception as e:
        print(f"[다운로드 실패] {e}")
        return None


def pexels_photo(query: str, out_dir: Path, name: str, orientation: str = "portrait") -> Media | None:
    if not query:
        return None
    data = _pexels_get("https://api.pexels.com/v1/search",
                       {"query": query, "per_page": 5, "orientation": orientation})
    if not data or not data.get("photos"):
        return None
    p = data["photos"][0]
    out = _download(p["src"]["large2x"], out_dir / f"{name}.jpg")
    return Media(out, "photo", f"Photo: {p.get('photographer', '')} / Pexels", "stock") if out else None


def pexels_video(query: str, out_dir: Path, name: str) -> Media | None:
    if not query:
        return None
    data = _pexels_get("https://api.pexels.com/videos/search",
                       {"query": query, "per_page": 5, "orientation": "landscape"})
    if not data or not data.get("videos"):
        return None
    v = data["videos"][0]
    files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("width")]
    if not files:
        return None
    # 1080~1920 폭에 가장 가까운 파일 (너무 큰 4K는 피함)
    files.sort(key=lambda f: abs(f["width"] - 1600))
    out = _download(files[0]["link"], out_dir / f"{name}.mp4")
    user = (v.get("user") or {}).get("name", "")
    return Media(out, "video", f"Video: {user} / Pexels", "stock") if out else None


# ---------- X(트위터) / 인스타 / 유튜브 쇼츠 / 틱톡 링크에서 영상 받기 ----------

def download_social(url: str, out_dir: Path) -> tuple[Media | None, str, str]:
    """returns (영상, 게시물 글, 출처 이름). 실패하면 (None, "", "")."""
    try:
        import yt_dlp
    except ImportError:
        return None, "", ""
    from ffbin import FFMPEG
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(out_dir / "social_%(id)s.%(ext)s"),
        "format": "best[ext=mp4][height<=1920]/bv*[ext=mp4]+ba[ext=m4a]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": 200 * 1024 * 1024,
        "ffmpeg_location": FFMPEG,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info.get("_type") == "playlist" and info.get("entries"):
                info = info["entries"][0]
            path = Path(ydl.prepare_filename(info))
    except Exception as e:
        print(f"[영상 다운로드 실패] {url}: {e}")
        return None, "", ""
    if not path.exists():
        cands = sorted(out_dir.glob(f"social_{info.get('id')}.*"))
        path = cands[0] if cands else None
    who = info.get("uploader_id") or info.get("uploader") or ""
    site = (info.get("extractor_key") or "").lower()
    name = f"X @{who}" if "twitter" in site else (f"@{who}" if who else site)
    text = info.get("description") or info.get("title") or ""
    if not path or detect_kind(path) != "video":
        return None, text, name
    return Media(path, "video", f"Visual reference: {name}"), text, name


# ---------- X(트위터): fxtwitter 공개 API로 영상/사진 직접 받기 (로그인 불필요) ----------

X_STATUS = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter|fxtwitter|vxtwitter|fixupx|fixvx|twittpr)"
                      r"\.com/([^/\s?#]+)/status(?:es)?/(\d+)", re.I)


def fetch_x(url: str, out_dir: Path) -> tuple[list[Media], str, str]:
    """returns (영상 또는 사진 목록, 트윗 글, 출처 이름)."""
    m = X_STATUS.search(url)
    if not m:
        return [], "", ""
    user, sid = m.groups()
    try:
        r = httpx.get(f"https://api.fxtwitter.com/{user}/status/{sid}", timeout=20,
                      headers={"User-Agent": "Mozilla/5.0 igbot"})
        tweet = r.json().get("tweet") or {}
    except Exception as e:
        print(f"[X 조회 실패] {url}: {e}")
        return [], "", f"X @{user}"
    text = tweet.get("text") or ""
    name = f"X @{(tweet.get('author') or {}).get('screen_name') or user}"
    credit = f"Visual reference: {name}"
    media = tweet.get("media") or {}
    items = media.get("all") or ((media.get("photos") or []) + (media.get("videos") or []))
    out_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for i, it in enumerate(items[:10]):  # 트윗에 올라온 순서 그대로 (사진/영상 섞여도)
        url = it.get("url")
        if not url:
            continue
        is_video = it.get("type") in ("video", "gif")
        p = _download(url, out_dir / f"x_{sid}_{i}.{'mp4' if is_video else 'jpg'}")
        if p:
            result.append(Media(p, "video" if is_video else "photo", credit))
    return result, text, name


def normalize_x(url: str) -> str:
    m = X_STATUS.search(url)
    return f"https://x.com/{m.group(1)}/status/{m.group(2)}" if m else url


def fetch_x_meta(url: str) -> tuple[list[Media], str, str]:
    """영상은 이미 있을 때: 트윗 글과 작성자만."""
    mm = X_STATUS.search(url)
    if not mm:
        return [], "", ""
    user, sid = mm.groups()
    try:
        r = httpx.get(f"https://api.fxtwitter.com/{user}/status/{sid}", timeout=20,
                      headers={"User-Agent": "Mozilla/5.0 igbot"})
        t = r.json().get("tweet") or {}
        return [], t.get("text") or "", f"X @{(t.get('author') or {}).get('screen_name') or user}"
    except Exception:
        return [], "", f"X @{user}"
