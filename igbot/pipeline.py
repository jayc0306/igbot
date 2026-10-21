"""원고 생성 → 사진/영상 매칭 → 렌더링. bot.py에서 호출."""
import asyncio
import shutil
from pathlib import Path

import config
import content
import reel
import render
from media import (X_STATUS, Media, _download, as_photo, download_social, fetch_x, normalize_x,
                   stock_photo, stock_video)


MAX_CARD_MEDIA = 9  # 커버 + 본문 최대 9장 + 마지막 장 = 10장 (인스타 캐러셀 한도)


def _card_items(src: list[Media]) -> list[Media]:
    return src[:MAX_CARD_MEDIA]


def _card_note(src: list[Media]) -> str:
    items = _card_items(src)
    n = len(items)
    if n >= 2:
        return (f"첨부 미디어 {n}개가 순서대로 이미지로 주어진다 (영상은 한 장면 캡처). "
                f"커버는 1번 미디어 위에, 본문 k번째 슬라이드는 (k+1)번 미디어 위에 올라간다. "
                f"slides 는 정확히 {n - 1}장. 각 장 문구는 그 미디어에 보이는 장면과 맞게 써라.")
    if n == 1:
        return ("첨부 미디어 1개 (커버에 쓰인다). slides 는 3장. "
                "본문 슬라이드 사진은 image_query로 스톡 사진을 찾으니 장면이 떠오르는 키워드를 넣어라.")
    return "첨부 미디어 없음. slides 는 3~4장. 각 장 image_query로 스톡 사진을 찾는다."


def _reel_note(src: list[Media]) -> str:
    if not src:
        return ""
    p = sum(1 for m in src if m.kind == "photo")
    return (f"첨부 미디어: 사진 {p}장, 영상 {len(src) - p}개. 이 미디어가 그대로 화면에 쓰인다. "
            "첨부 이미지 내용을 참고해서 장면과 맞는 문구를 써라.")


async def _card_media(data: dict, src: list[Media], out: Path) -> list[Media | None]:
    """[커버, 본문1, 본문2...] 순서로 미디어 배정. 같은 미디어를 두 번 쓰지 않음."""
    items = _card_items(src)
    n = 1 + len(data["slides"])
    queries = [None] + [s.get("image_query") for s in data["slides"]]
    has_stock = bool(config.PIXABAY_API_KEY or config.PEXELS_API_KEY)
    res: list[Media | None] = []
    for i in range(n):
        if i < len(items):
            res.append(items[i])
        elif has_stock and queries[i]:
            res.append(await asyncio.to_thread(stock_photo, queries[i], out, f"px_{i}", "landscape"))
        else:
            res.append(None)  # 사진 없으면 어두운 기본 배경
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


def _render_card(data: dict, media: list[Media | None], out: Path) -> list[Path]:
    import card
    paths = [card.make_cover(media[0] if media else None, data.get("cover_sub", ""),
                             data.get("cover_title", ""), out / "01_cover")]
    for i, s in enumerate(data["slides"], start=1):
        m = media[i] if i < len(media) else None
        paths.append(card.make_slide(m, s.get("kicker", ""), s.get("heading", ""), s.get("body", ""),
                                     out / f"{i + 1:02d}_slide"))
    if config.OUTRO_ENABLED and len(paths) < 10:
        first = media[0] if media else None
        bg = as_photo(first, out).path if first else None
        p = out / f"{len(paths) + 1:02d}_outro.jpg"
        render.card_outro(bg, data.get("share_target", ""), p)
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
    got, text = [], ""
    if x_links:
        # 트윗 원본의 사진/영상을 올라온 순서 그대로 받음 (포워드에 붙은 것보다 원본이 정확)
        await say("📥 트윗에서 사진/영상 받는 중…")
        got, text, author = await asyncio.to_thread(fetch_x, x_links[0], job["dir"] / "src")
        if got:
            src[:] = got + [m for m in src if m.origin != "source"]
    if social and not got and not any(m.kind == "video" for m in src):
        await say("📥 링크에서 영상 받는 중…")
        v, t2, w2 = await asyncio.to_thread(download_social, normalize_x(social[0]), job["dir"] / "src")
        if v:
            src.insert(0, v)
        text, author = text or t2, author or w2
    if text:
        job["raw"] += f"\n\n[원본 게시물 글]\n{text}"
    if social and not src:
        job.setdefault("warnings", []).append(
            "링크에서 사진/영상을 못 받았어요. 폰에 저장해서 파일로 보내주시면 그걸로 만들게요")

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
        shown = _card_items(src) if kind == "card" else src[:2]
        look = [await asyncio.to_thread(as_photo, m, out) for m in shown]
        prev = job.get("data") if feedback else None
        note = _card_note(src) if kind == "card" else _reel_note(src)
        data = await asyncio.to_thread(content.generate, kind, job["source"], note,
                                       [m.path for m in look], feedback, prev)
        job["data"] = data
    job["caption"] = content.build_caption(data, job.get("sources"))

    if kind == "card":
        has_video = any(m.kind == "video" for m in _card_items(src))
        await say("🖼 카드 만드는 중…" + (" (영상 장이 있어서 1~2분)" if has_video else ""))
        media = await _card_media(data, src, out)
        job["files"] = await asyncio.to_thread(_render_card, data, media, out)
    else:
        await say("🎬 릴스 만드는 중… (1~2분)")
        if config.REEL_SUBTITLES:
            scene_media = await _reel_media(data, src, out)
            video, cover = await reel.build_reel(data, out, scene_media)
        else:
            media_list = list(src)  # 올라온 순서 그대로 (사진/영상 섞여도)
            if not media_list:
                q = data.get("image_query")
                m = (await asyncio.to_thread(stock_video, q, out, "pxv")
                     or await asyncio.to_thread(stock_photo, q, out, "px", "landscape"))
                media_list = [m] if m else []
            video, cover = await reel.build_framed(data, out, media_list)
        job["files"] = [video, cover]
