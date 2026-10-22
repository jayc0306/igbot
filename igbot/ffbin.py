"""ffmpeg 위치 찾기. PC에 설치된 게 없으면 파이썬 패키지(imageio-ffmpeg)에 들어있는 걸 씀."""
import re
import shutil
import subprocess
from pathlib import Path


def _find() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise RuntimeError("ffmpeg를 찾을 수 없어요. setup.bat을 다시 실행해 주세요.")


FFMPEG = _find()
_DUR = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def duration(path: Path) -> float:
    """영상/음성 길이(초). ffprobe 없이 ffmpeg 출력에서 읽음."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                       text=True, encoding="utf-8", errors="ignore")
    m = _DUR.search(r.stderr)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def has_audio(path: Path) -> bool:
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                       text=True, encoding="utf-8", errors="ignore")
    return "Audio:" in r.stderr


_SIZE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")


def video_size(path: Path) -> tuple[int, int]:
    """영상 가로x세로 (세로로 찍은 폰 영상의 회전 정보까지 반영)."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                       text=True, encoding="utf-8", errors="ignore")
    m = _SIZE.search(r.stderr)
    if not m:
        return 0, 0
    w, h = int(m.group(1)), int(m.group(2))
    if re.search(r"rotat\w* (?:of )?-?(90|270)", r.stderr):
        w, h = h, w
    return w, h
