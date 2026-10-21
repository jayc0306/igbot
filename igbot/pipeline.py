"""원고 생성 → 사진/영상 매칭 → 렌더링. bot.py에서 호출."""
import asyncio
import shutil
from pathlib import Path

import config
import content
import reel
import render
from media import (X_STATUS, Media, _download, as_photo, download_social, fetch_x, fetch_x_meta, normalize_x,
                   stock_photo, stock_video)


def _media_note(src: list[Media]) -> str:
    if not src:
        return ""
    p = sum(1 for m in src if m.kind == "photo")
    v = len(src) - p
    return (f"첨부 미디어: 사진 {p}장, 영상 {v}개. 이 미디어가 그대로 화면에 쓰인다. "
            "첨부 사진이 함께 주어지면 내용을 참고해서 장면과 맞는 문구를 써라.")


async def _card_photos(data: dict, src: list[Media], out: Path) -> list[Media | None]:
    slides = data["slides"]
    n = 1 + len(slides)
    photos = [await asyncio.to_thread(as_photo, m, out) for m in src]
    queries = [data.get("cover_image_query")] + [s.get("image_query") for s in slides]
    res: list[Media | None] = []
    has_stock = bool(config.PIXABAY_API_KEY or config.PEXELS_API_KEY)
    for i in range(n):
        if i < len(photos):
            res.append(photos[i])
            continue
        m = await asyncio.to_thread(stock_photo, queries[i], out, f"px_{i}", "portrait") if has_stock else None
        if not m and photos:
            m = photos[i % len(photos)]  # 스톡이 없으면 소스 사진 돌려쓰기
        res.append(m)
    return res


async def _reel_media(data: dict, src: list[Media], out: Path) -> list[Media | None]:
    scenes = data["scenes"]
    videos = [m for m in src if m.kind == "video"]
    photos = [m for m in src if m.kind == "photo"]
    res: list[Media | None] = []
    for i, s in enumerate(scenes):
        if videos:
            res.append(videos[i * len(videos) // len(scenes)])  # 영상 여러 개면 순서대로 구간 배분
        elif photos:
            res.append(photos[i % len(photos)])
        else:
            q = s.get("image_query")
            m = await asyncio.to_thread(stock_video, q, out, f"pxv_{i}")
            if not m:
                m = await asyncio.to_thread(stock_photo, q, out, f"px_{i}", "landscape")
            res.append(m)
    return res


def _render_card(data: dict, photos: list[Media | None], out: Path) -> list[Path]:
    slides = data["slides"]
    has_outro = config.OUTRO_ENABLED
    paths = []

    def ph(i):
        m = photos[i] if i < len(photos) else None
        return (m.path, m.credit) if m else (None, "")

    p = out / "01_cover.jpg"
    path, credit = ph(0)
    render.card_cover(path, data["cover_title"], p, credit)
    paths.append(p)
    for i, s in enumerate(slides, start=1):
        p = out / f"{i + 1:02d}_slide.jpg"
        path, credit = ph(i)
        render.card_slide(i, len(slides), path, s["heading"], s["body"], p, credit)
        paths.append(p)
    if has_outro and len(paths) < 10:
        p = out / f"{len(paths) + 1:02d}_outro.jpg"
        render.card_outro(ph(0)[0], data.get("share_target", ""), p)
        paths.append(p)
    return paths[:10]


def _apply_sources(job: dict):
    """출처 정리. 사용자가 직접 지정한 출처가 있으면 그게 최우선."""
    ov = job.get("source_override")
    if ov is None:
        return
    job["sources"] = [ov] if ov else []
    for m in job.get("media", []):
        if m.origin == "source":
            m.credit = f"Visual reference: {ov}" if ov else ""


async def _gather_sources(job: dict, say):
    """링크에서 영상/글/기사 사진을 받아오고, 출처를 원본 작성자 기준으로 정리 (처음 한 번만)."""
    src: list[Media] = job["media"]
    urls = list(dict.fromkeys(content.URL_RE.findall(job["raw"]) + job.get("urls", [])))
    x_links = [u for u in urls if X_STATUS.search(u)]
    print(f"[소스] 링크 {urls} / 첨부 {[m.kind for m in src]}")
    social = x_links + [u for u in urls if any(h in u for h in content.SOCIAL_HOSTS) and u not in x_links]

    author = ""
    if social:
        need_video = not any(m.kind == "video" for m in src)
        if need_video:
            await say("📥 링크에서 영상 받는 중…")
        got, text, who = [], "", ""
        if x_links:
            got, text, who = await asyncio.to_thread(fetch_x, x_links[0], job["dir"] / "src") if need_video \
                else await asyncio.to_thread(fetch_x_meta, x_links[0])
        if need_video and not any(g.kind == "video" for g in got):
            v, t2, w2 = await asyncio.to_thread(download_social, normalize_x(social[0]), job["dir"] / "src")
            if v:
                got = [v]
            text, who = text or t2, who or w2
        src[:0] = got
        author = who
        if text:
            job["raw"] += f"\n\n[원본 게시물 글]\n{text}"
        if need_video and not any(g.kind == "video" for g in got):
            job.setdefault("warnings", []).append(
                "링크에서 영상을 못 받았어요. 영상을 폰에 저장해서 파일로 보내주시면 그걸로 만들게요")

    await say("🔗 소스 읽는 중…")
    job["source"], images, domains = await asyncio.to_thread(content.build_source, job["raw"], job.get("urls"))
    for k, (url, domain) in enumerate(images[:2]):
        path = await asyncio.to_thread(_download, url, job["dir"] / "src" / f"article_{k}.jpg")
        if path:
            src.append(Media(path, "photo", f"사진: {domain}", "article"))

    # 출처 우선순위: 원본 게시물 작성자(X 등) > 기사 사이트 > 포워드한 채널/사람
    if author:
        job["sources"] = [author]
        for m in src:
            if m.origin == "source":
                m.credit = f"Visual reference: {author}"
    elif domains:
        job["sources"] = domains


async def make(job: dict, feedback: str | None = None, progress=None, rerender: bool = False):
    """job에 data, files, caption 채움. rerender=True면 원고는 그대로 두고 이미지/영상만 다시 만듦."""
    async def say(t):
        if progress:
            await progress(t)

    kind = job["kind"]
    src: list[Media] = job.setdefault("media", [])
    out: Path = job["dir"] / "out"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    if job.get("source") is None:
        await _gather_sources(job, say)
    _apply_sources(job)

    if rerender and job.get("data"):
        data = job["data"]
    else:
        await say(f"✍️ {'카드뉴스' if kind == 'card' else '릴스'} 원고 작성 중…")
        look = [await asyncio.to_thread(as_photo, m, out) for m in src[:2]]
        prev = job.get("data") if feedback else None
        data = await asyncio.to_thread(content.generate, kind, job["source"], _media_note(src),
                                       [m.path for m in look], feedback, prev)
        job["data"] = data
    job["caption"] = content.build_caption(data, job.get("sources"))

    if kind == "card":
        await say("🖼 사진 맞추고 카드 만드는 중…")
        photos = await _card_photos(data, src, out)
        job["files"] = await asyncio.to_thread(_render_card, data, photos, out)
    else:
        await say("🎬 릴스 만드는 중… (1~2분)")
        if config.REEL_SUBTITLES:
            scene_media = await _reel_media(data, src, out)
            video, cover = await reel.build_reel(data, out, scene_media)
        else:
            media_list = [m for m in src if m.kind == "video"] or [m for m in src if m.kind == "photo"]
            if not media_list:
                q = data.get("image_query")
                m = (await asyncio.to_thread(stock_video, q, out, "pxv")
                     or await asyncio.to_thread(stock_photo, q, out, "px", "landscape"))
                media_list = [m] if m else []
            video, cover = await reel.build_framed(data, out, media_list)
        job["files"] = [video, cover]
