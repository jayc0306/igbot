"""카드뉴스 렌더링 (레퍼런스: today.worthy 스타일).

- 커버: 사진/영상 꽉 채움 + 하단 그라데이션 + 가운데 2단 제목 + 브랜드 워드마크
- 본문: 위쪽 사진/영상, 아래 검은 영역에 라벨/소제목/본문 + 워드마크
- 장마다 미디어 1개씩 (같은 사진 돌려쓰지 않음). 영상이면 그 장은 mp4로 만듦
"""
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

import config
import render
from ffbin import FFMPEG, duration, has_audio, video_size
from media import Media

W, H = render.CARD_W, render.CARD_H
SLIDE_MEDIA_H = 860
COVER_FG_H = 980  # 커버에서 가로 사진은 이 높이 안에 통째로 (아래는 제목 자리)
MAX_VIDEO_SEC = 60
TG_LIMIT = 45 * 1024 * 1024


# ---------- 공통 ----------

def wordmark(d: ImageDraw.ImageDraw, cy: float, size: int = 46):
    """thecontext.kr → 점(.)만 강조색."""
    name = config.BRAND_NAME
    f = font_b = render.font("Black", size)
    parts = name.split(".", 1)
    total = f.getlength(name)
    x = (W - total) / 2
    if len(parts) == 2:
        d.text((x, cy), parts[0], font=font_b, fill="#FFFFFF")
        x += f.getlength(parts[0])
        d.text((x, cy), ".", font=font_b, fill=config.COLOR_ACCENT)
        x += f.getlength(".")
        d.text((x, cy), parts[1], font=font_b, fill="#FFFFFF")
    else:
        d.text((x, cy), name, font=font_b, fill="#FFFFFF")


def _gradient(overlay: Image.Image, y0: int, y1: int, max_alpha: int = 255):
    d = ImageDraw.Draw(overlay)
    for y in range(y0, y1):
        t = (y - y0) / max(1, y1 - y0)
        d.line([(0, y), (W, y)], fill=(0, 0, 0, int(max_alpha * (t ** 1.3))))


def _credit(d, text: str):
    if not text:
        return
    f = render.font("Regular", 22)
    text = text[:60]
    x = W - 36 - f.getlength(text)
    d.text((x + 1, 31), text, font=f, fill=(0, 0, 0, 160))
    d.text((x, 30), text, font=f, fill=(235, 235, 235, 230))


# ---------- 레이어(텍스트) 만들기 ----------

def cover_overlay(sub: str, title: str, credit: str) -> Image.Image:
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _gradient(ov, 560, H, 245)
    d = ImageDraw.Draw(ov)

    tf, tlines, ts, th = render.fit_text(title, "Black", 92, 60, W - 100, 240, lh=1.2)
    sub = (sub or "").strip()
    sf, slines, ss, sh = render.fit_text(sub, "Medium", 46, 34, W - 120, 130, lh=1.3) if sub else (None, [], 0, 0)

    bottom = 1150
    ty = bottom - th
    sy = ty - sh - 14
    if sub:
        render.draw_block(d, 60, sy, slines, sf, ss, 1.3, (240, 240, 240, 255), config.COLOR_ACCENT,
                          align="center", box_w=W - 120)
    render.draw_block(d, 50, ty, tlines, tf, ts, 1.2, "#FFFFFF", config.COLOR_ACCENT,
                      align="center", box_w=W - 100)
    wordmark(d, 1205, 50)
    _credit(d, credit)
    return ov


def slide_overlay(kicker: str, heading: str, body: str, credit: str) -> Image.Image:
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _gradient(ov, SLIDE_MEDIA_H - 170, SLIDE_MEDIA_H, 255)
    ImageDraw.Draw(ov).rectangle((0, SLIDE_MEDIA_H, W, H), fill=(0, 0, 0, 255))
    d = ImageDraw.Draw(ov)
    x, box = 66, W - 132

    hf, hlines, hs, hh = render.fit_text(heading, "Black", 68, 48, box, 180, lh=1.22)
    paras = [p.strip() for p in (body or "").replace("\\n", "\n").split("\n") if p.strip()]
    bs = 36
    while True:
        bf = render.font("Regular", bs)
        blines = []
        for p in paras:
            blines += render.rich_wrap(p, bf, box)
        bh = int(len(blines) * bs * 1.55)
        if bh <= 170 or bs <= 28:
            break
        bs -= 2
    kf = render.font("Bold", 32)
    kh = 32 + 20 if kicker else 0

    bottom = 1150
    y = bottom - (kh + hh + (26 + bh if blines else 0))
    if kicker:
        d.text((x, y), kicker, font=kf, fill=config.COLOR_ACCENT)
        y += kh
    render.draw_block(d, x, y, hlines, hf, hs, 1.22, "#FFFFFF", config.COLOR_ACCENT)
    y += hh + 26
    render.draw_block(d, x, y, blines, bf, bs, 1.55, (205, 205, 205, 255), config.COLOR_ACCENT)

    wordmark(d, 1222, 42)
    _credit(d, credit)
    return ov


# ---------- 합성 ----------

def _photo(media: Media | None, box: tuple[int, int, int, int], ov: Image.Image, out: Path,
           fg_h: int | None = None) -> Path:
    x, y, w, h = box
    base = Image.new("RGB", (W, H), (0, 0, 0))
    if media:
        base.paste(render.smart_fit(media.path, w, h, fg_h), (x, y))
    else:
        base.paste(render.fallback_bg(w, h), (x, y))
    img = Image.alpha_composite(base.convert("RGBA"), ov).convert("RGB")
    out = out.with_suffix(".jpg")
    img.save(out, "JPEG", quality=94)
    return out


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 실패: {r.stderr[-600:]}")


def media_filter(path: Path, w: int, h: int, fg_h: int | None = None, inp: str = "1:v") -> str:
    """ffmpeg 필터: 비율 비슷하면 꽉 채우기, 아니면 영상 전체 + 흐린 배경. 결과 라벨 [m]."""
    vw, vh = video_size(path)
    fg_h = fg_h or h
    if not render.needs_contain(vw, vh, w, h):
        return f"[{inp}]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30[m]"
    return (f"[{inp}]split[va][vb];"
            f"[va]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=24:2,"
            f"eq=brightness=-0.18,setsar=1[vbg];"
            f"[vb]scale={w}:{fg_h}:force_original_aspect_ratio=decrease,setsar=1[vfg];"
            f"[vbg][vfg]overlay=(main_w-overlay_w)/2:({fg_h}-overlay_h)/2,fps=30[m]")


def _video(media: Media, box: tuple[int, int, int, int], ov: Image.Image, out: Path,
           fg_h: int | None = None) -> Path:
    x, y, w, h = box
    ov_path = out.with_name(out.stem + "_ov.png")
    ov.save(ov_path)
    out = out.with_suffix(".mp4")
    vd = duration(media.path) or 5.0
    dur = min(vd, MAX_VIDEO_SEC)
    cut = vd > dur + 0.5
    audio = has_audio(media.path) and config.KEEP_SOURCE_AUDIO
    fade = f",afade=t=out:st={max(0, dur - 1.2):.2f}:d=1.2" if cut else ""
    a_in = [] if audio else ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    a_f = (f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo{fade}[a]" if audio else "[3:a]anull[a]")
    _run([FFMPEG, "-y", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=30",
          "-i", str(media.path), "-loop", "1", "-i", str(ov_path), *a_in,
          "-filter_complex",
          f"{media_filter(media.path, w, h, fg_h)};"
          f"[0:v][m]overlay={x}:{y}:eof_action=repeat[b];[b][2:v]overlay=0:0,format=yuv420p[v];{a_f}",
          "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "6M", "-bufsize", "12M",
          "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)])
    if out.stat().st_size > TG_LIMIT:  # 텔레그램 전송 한도 맞추기
        kbps = int(TG_LIMIT * 8 / 1024 / dur) - 200
        small = out.with_name(out.stem + "_s.mp4")
        _run([FFMPEG, "-y", "-i", str(out), "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{kbps}k",
              "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k", "-pix_fmt", "yuv420p", "-c:a", "copy",
              "-movflags", "+faststart", str(small)])
        small.replace(out)
    ov_path.unlink(missing_ok=True)
    return out


def make_cover(media: Media | None, sub: str, title: str, out: Path) -> Path:
    ov = cover_overlay(sub, title, media.credit if media else "")
    box = (0, 0, W, H)
    if media and media.kind == "video":
        return _video(media, box, ov, out, COVER_FG_H)
    return _photo(media, box, ov, out, COVER_FG_H)


def make_slide(media: Media | None, kicker: str, heading: str, body: str, out: Path) -> Path:
    ov = slide_overlay(kicker, heading, body, media.credit if media else "")
    box = (0, 0, W, SLIDE_MEDIA_H)
    if media and media.kind == "video":
        return _video(media, box, ov, out)
    return _photo(media, box, ov, out)
