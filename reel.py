"""릴스 영상 생성.

레이아웃: 검은 배경 + 상단 라벨/제목(고정) + 가운데 영상/사진(장면별) + 자막 박스 + 하단 브랜드
같은 원본 영상을 여러 장면에 쓰면 이어서 재생되도록 시작 지점을 넘겨줌.
"""
import asyncio
import subprocess
from pathlib import Path

import config
from ffbin import FFMPEG, duration, has_audio
from card import media_filter
import render
from media import Media, video_duration

FPS = 30
TG_LIMIT = 45 * 1024 * 1024  # 텔레그램 봇 전송 한도(50MB)보다 살짝 작게


def _run(cmd: list[str]):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 실패: {r.stderr[-800:]}")


async def _tts(text: str, path: Path) -> bool:
    try:
        import edge_tts

        await edge_tts.Communicate(text, config.TTS_VOICE, rate=config.TTS_RATE).save(str(path))
        return path.exists() and path.stat().st_size > 0
    except Exception as e:
        print(f"[TTS 실패, 무음으로 진행] {e}")
        return False


def _scene_clip(bg: Path, media_y: int, media: Media, overlay: Path, audio: Path | None,
                dur: float, start: float, out: Path):
    W, H = render.REEL_W, render.REEL_MEDIA_H
    frames = int(dur * FPS) + 2
    if media.kind == "video":
        m_in = ["-stream_loop", "-1", "-ss", f"{start:.2f}", "-i", str(media.path)]
        m_f = media_filter(media.path, W, H)
    else:
        panel = out.with_name(out.stem + "_panel.jpg")
        render.smart_fit(media.path, W, H).save(panel, "JPEG", quality=93)
        m_in = ["-i", str(panel)]
        bw, bh = int(W * 1.5), int(H * 1.5)
        m_f = (f"[1:v]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
               f"zoompan=z='min(zoom+0.0004,1.05)':d={frames}:x='iw/2-(iw/zoom/2)':"
               f"y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},setsar=1[m]")
    a_in = ["-i", str(audio)] if audio else ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]

    _run([FFMPEG, "-y", "-loop", "1", "-i", str(bg), *m_in, "-loop", "1", "-i", str(overlay), *a_in,
          "-filter_complex",
          f"{m_f};[0:v][m]overlay=0:{media_y}[b];[b][2:v]overlay=0:0,format=yuv420p[v];[3:a]apad[a]",
          "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}", "-r", str(FPS),
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
          "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k", str(out)])


def _probe_audio(path: Path) -> float:
    return video_duration(path)


async def build_reel(data: dict, out_dir: Path, scene_media: list[Media | None]) -> tuple[Path, Path]:
    """returns (video_path, cover_image_path)"""
    out_dir.mkdir(parents=True, exist_ok=True)
    bg = out_dir / "bg.png"
    media_y = render.reel_background(data.get("label", ""), data["title"], bg)

    fallback = out_dir / "fallback.jpg"
    render.reel_fallback_panel(fallback)

    offsets: dict[Path, float] = {}
    clips = []
    for i, s in enumerate(data["scenes"]):
        m = scene_media[i] if i < len(scene_media) and scene_media[i] else Media(fallback, "photo")
        ov = out_dir / f"ov_{i:02d}.png"
        render.reel_overlay(s.get("subtitle", ""), m.credit, media_y, ov)

        audio = None
        narration = (s.get("narration") or "").replace("**", "").strip()
        if config.TTS_ENABLED and narration:
            a = out_dir / f"tts_{i:02d}.mp3"
            if await _tts(narration, a):
                audio = a
        if audio:
            dur = _probe_audio(audio) + 0.3
        else:
            dur = max(2.5, min(6.0, len(s.get("subtitle", "")) * 0.14 + 1.2))

        start = 0.0
        if m.kind == "video":
            vd = video_duration(m.path) or 1.0
            start = offsets.get(m.path, 0.0) % vd
            offsets[m.path] = offsets.get(m.path, 0.0) + dur

        clip = out_dir / f"scene_{i:02d}.mp4"
        await asyncio.to_thread(_scene_clip, bg, media_y, m, ov, audio, dur, start, clip)
        clips.append(clip)

    return await _finish(clips, out_dir)


async def _finish(clips: list[Path], out_dir: Path) -> tuple[Path, Path]:
    """장면 클립 이어붙이기 + BGM + 커버 추출."""
    listfile = out_dir / "clips.txt"
    listfile.write_text("".join(f"file '{c.name}'\n" for c in clips))
    joined = out_dir / "joined.mp4"
    await asyncio.to_thread(_run, [FFMPEG, "-y", "-f", "concat", "-safe", "0",
                                   "-i", str(listfile), "-c", "copy", str(joined)])

    final = out_dir / "reel.mp4"
    if config.BGM_PATH and Path(config.BGM_PATH).exists():
        await asyncio.to_thread(_run, [
            FFMPEG, "-y", "-i", str(joined), "-stream_loop", "-1", "-i", config.BGM_PATH,
            "-filter_complex",
            f"[1:a]volume={config.BGM_VOLUME}[b];"
            f"[0:a][b]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(final)])
    else:
        await asyncio.to_thread(_run, [FFMPEG, "-y", "-i", str(joined), "-c", "copy",
                                       "-movflags", "+faststart", str(final)])

    # 텔레그램 봇은 50MB 넘는 파일을 못 보냄 → 넘으면 비트레이트 맞춰 다시 압축
    limit = TG_LIMIT
    if final.stat().st_size > limit:
        dur = duration(final) or 60
        v_kbps = int(limit * 8 / 1024 / dur) - 200
        small = out_dir / "reel_small.mp4"
        await asyncio.to_thread(_run, [
            FFMPEG, "-y", "-i", str(final), "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{v_kbps}k", "-maxrate", f"{v_kbps}k", "-bufsize", f"{v_kbps * 2}k",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(small)])
        small.replace(final)

    cover = out_dir / "cover.jpg"
    await asyncio.to_thread(_run, [FFMPEG, "-y", "-ss", "0.5", "-i", str(final),
                                   "-frames:v", "1", "-q:v", "2", str(cover)])
    return final, cover


def _framed_clip(bg: Path, media_y: int, media: Media, overlay: Path, dur: float, source_audio: bool,
                 out: Path, cut: bool = False):
    """자막 없는 버전: 원본 영상(소리 포함)을 프레임 안에 그대로."""
    W, H = render.REEL_W, render.REEL_MEDIA_H
    frames = int(dur * FPS) + 2
    if media.kind == "video":
        m_in = ["-i", str(media.path)]
        m_f = media_filter(media.path, W, H)
    else:
        panel = out.with_name(out.stem + "_panel.jpg")
        render.smart_fit(media.path, W, H).save(panel, "JPEG", quality=93)
        m_in = ["-i", str(panel)]
        bw, bh = int(W * 1.5), int(H * 1.5)
        m_f = (f"[1:v]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
               f"zoompan=z='min(zoom+0.0004,1.05)':d={frames}:x='iw/2-(iw/zoom/2)':"
               f"y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},setsar=1[m]")
    fade = f",afade=t=out:st={max(0, dur - 1.2):.2f}:d=1.2" if cut else ""  # 잘린 영상은 소리 자연스럽게 줄이기
    if source_audio:
        a_in, a_f = [], f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo{fade},apad[a]"
    else:
        a_in, a_f = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"], "[3:a]apad[a]"
    _run([FFMPEG, "-y", "-loop", "1", "-i", str(bg), *m_in, "-loop", "1", "-i", str(overlay), *a_in,
          "-filter_complex",
          f"{m_f};[0:v][m]overlay=0:{media_y}:eof_action=repeat[b];[b][2:v]overlay=0:0,format=yuv420p[v];{a_f}",
          "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}", "-r", str(FPS),
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "6M", "-bufsize", "12M",
          "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k", str(out)])


async def build_framed(data: dict, out_dir: Path, media_list: list[Media]) -> tuple[Path, Path]:
    """상단 라벨/제목 + 가운데 원본 영상(자막 없음) + 하단 브랜드."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bg = out_dir / "bg.png"
    media_y = render.reel_background(data.get("label", ""), data["title"], bg)
    if not media_list:
        fb = out_dir / "fallback.jpg"
        render.reel_fallback_panel(fb)
        media_list = [Media(fb, "photo")]

    photo_sec = config.REEL_PHOTO_SEC if len(media_list) > 1 else 6.0
    clips, total = [], 0.0
    for i, m in enumerate(media_list):
        left = config.REEL_MAX_SEC - total
        if left < 1:
            break
        ov = out_dir / f"ov_{i:02d}.png"
        render.reel_overlay("", m.credit, media_y, ov)
        cut = False
        if m.kind == "video":
            vd = video_duration(m.path) or 5.0
            dur = min(vd, left)
            cut = vd > left + 0.5
            src_audio = config.KEEP_SOURCE_AUDIO and has_audio(m.path)
        else:
            dur, src_audio = min(photo_sec, left), False
        clip = out_dir / f"clip_{i:02d}.mp4"
        await asyncio.to_thread(_framed_clip, bg, media_y, m, ov, dur, src_audio, clip, cut)
        clips.append(clip)
        total += dur
    return await _finish(clips, out_dir)
