"""이미지 렌더링 (레퍼런스: 이슈 매거진 스타일).

카드뉴스 1080x1350: 사진 풀블리드 + 하단 그라데이션 + 굵은 흰 제목
릴스 1080x1920: 검은 배경, 상단 라벨+제목 고정, 가운데 영상, 아래 브랜드 / 영상 위 자막 박스
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

import config

CARD_W, CARD_H = 1080, 1350
REEL_W, REEL_H = 1080, 1920
REEL_MEDIA_H = 840
PAD = 70

_font_cache: dict = {}


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    key = (weight, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(config.FONT_DIR / f"Pretendard-{weight}.otf"), size)
    return _font_cache[key]


def rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ---------- 리치 텍스트 (**강조**) ----------

def parse_rich(text: str) -> list[tuple[str, bool]]:
    out, hl, i = [], False, 0
    while i < len(text):
        if text.startswith("**", i):
            hl = not hl
            i += 2
            continue
        out.append((text[i], hl))
        i += 1
    return out


def _s(rich) -> str:
    return "".join(c for c, _ in rich)


def rich_wrap(text: str, f, max_w: int) -> list:
    rich = parse_rich(text.replace("\\n", "\n"))
    paras, cur = [], []
    for ch in rich:
        if ch[0] == "\n":
            paras.append(cur)
            cur = []
        else:
            cur.append(ch)
    paras.append(cur)

    lines = []
    for para in paras:
        words, w = [], []
        for ch in para:
            if ch[0] == " ":
                words.append(w)
                w = []
            else:
                w.append(ch)
        words.append(w)
        line: list = []
        for word in words:
            if not word:
                continue
            cand = line + [(" ", False)] + word if line else word
            if f.getlength(_s(cand)) <= max_w:
                line = cand
                continue
            if line:
                lines.append(line)
                line = []
            while f.getlength(_s(word)) > max_w:
                i = len(word)
                while i > 1 and f.getlength(_s(word[:i])) > max_w:
                    i -= 1
                lines.append(word[:i])
                word = word[i:]
            line = word
        lines.append(line)
    return lines


def draw_rich_line(d, xy, line, f, color, hl_color, hl_mode="color"):
    """hl_mode: color(강조 글자색) | box(강조 단어 뒤에 색 박스)"""
    x, y = xy
    seg, seg_hl = "", None
    for ch, hl in line + [("", None)]:
        if hl != seg_hl and seg:
            if seg_hl and hl_mode == "box":
                b = f.getbbox(seg)
                d.rectangle((x + b[0] - 8, y + b[1] - 8, x + b[2] + 8, y + b[3] + 10), fill=hl_color)
                d.text((x, y), seg, font=f, fill=color)
            else:
                d.text((x, y), seg, font=f, fill=hl_color if seg_hl else color)
            x += f.getlength(seg)
            seg = ""
        seg_hl = hl
        seg += ch


def fit_text(text, weight, max_size, min_size, max_w, max_h, lh=1.2):
    size = max_size
    while True:
        f = font(weight, size)
        lines = rich_wrap(text, f, max_w)
        h = int(len(lines) * size * lh)
        if h <= max_h or size <= min_size:
            return f, lines, size, h
        size -= 2


def draw_block(d, x, y, lines, f, size, lh, color, hl_color, align="left", box_w=None, hl_mode="color"):
    for i, line in enumerate(lines):
        lx = x
        if align == "center":
            lx = x + (box_w - f.getlength(_s(line))) / 2
        draw_rich_line(d, (lx, y + i * size * lh), line, f, color, hl_color, hl_mode)


# ---------- 이미지 유틸 ----------

def cover_fit(path: Path, w: int, h: int) -> Image.Image:
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    return ImageOps.fit(img, (w, h), Image.LANCZOS, centering=(0.5, 0.4))


def fallback_bg(w: int, h: int) -> Image.Image:
    """사진이 없을 때: 어두운 그라데이션 + 강조색 글로우."""
    img = Image.new("RGB", (w, h), (18, 18, 20))
    d = ImageDraw.Draw(img)
    for yy in range(h):
        c = int(18 + 22 * yy / h)
        d.line([(0, yy), (w, yy)], fill=(c, c, c + 2))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    r, g, b = rgb(config.COLOR_ACCENT)
    gd.ellipse((w * 0.45, -h * 0.25, w * 1.35, h * 0.45), fill=(r, g, b, 55))
    return Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")


def bottom_gradient(img: Image.Image, start_y: int, max_alpha: int = 235) -> Image.Image:
    w, h = img.size
    start_y = max(0, min(h - 1, start_y))
    col = Image.new("L", (1, h), 0)
    for yy in range(start_y, h):
        t = (yy - start_y) / max(1, h - start_y)
        col.putpixel((0, yy), int(max_alpha * min(1.0, t * 1.35) ** 1.2))
    mask = col.resize((w, h))
    return Image.composite(Image.new("RGB", (w, h), (0, 0, 0)), img, mask)


def _credit(d, text, x_right, y, size=22, fill=(200, 200, 200)):
    if not text:
        return
    f = font("Regular", size)
    text = text[:60]
    d.text((x_right - f.getlength(text), y), text, font=f, fill=fill)


def _brand_tag(d, x, y, size=30):
    f = font("Black", size)
    w = f.getlength(config.BRAND_NAME) + size
    d.rectangle((x, y, x + w, y + size * 1.55), fill=config.COLOR_ACCENT)
    d.text((x + size / 2, y + size * 0.2), config.BRAND_NAME, font=f, fill="#FFFFFF")


# ---------- 카드뉴스 ----------

def card_cover(photo: Path | None, title: str, out: Path, credit: str = ""):
    img = cover_fit(photo, CARD_W, CARD_H) if photo else fallback_bg(CARD_W, CARD_H)
    tf, lines, size, th = fit_text(title, "Black", 124, 72, CARD_W - PAD * 2, 470, lh=1.16)
    ty = 1215 - th
    img = bottom_gradient(img, int(ty - 330))
    d = ImageDraw.Draw(img)
    _brand_tag(d, PAD, ty - 76)
    draw_block(d, PAD, ty, lines, tf, size, 1.16, config.COLOR_TEXT, config.COLOR_ACCENT)
    d.text((PAD, 1272), config.BRAND_HANDLE, font=font("Bold", 26), fill=(220, 220, 220))
    _credit(d, credit, CARD_W - PAD, 1276)
    img.save(out, "JPEG", quality=94)


def card_slide(idx: int, total: int, photo: Path | None, heading: str, body: str, out: Path,
               credit: str = ""):
    paras = [p.strip() for p in body.replace("\\n", "\n").split("\n") if p.strip()]
    max_w = CARD_W - PAD * 2

    hf, hlines, hsize, hh = fit_text(heading, "Black", 72, 50, max_w, 240, lh=1.2)
    size = 40
    while True:
        bf = font("Medium", size)
        blocks = [rich_wrap(p, bf, max_w) for p in paras]
        gap = int(size * 0.6)
        bh = int(sum(len(b) * size * 1.55 for b in blocks) + gap * max(0, len(blocks) - 1))
        if bh <= (520 if photo else 700) or size <= 28:
            break
        size -= 2

    if photo:
        top = 1225 - (hh + 44 + bh)
        img = bottom_gradient(cover_fit(photo, CARD_W, CARD_H), int(top - 300), 245)
    else:
        top = 230
        img = Image.new("RGB", (CARD_W, CARD_H), rgb(config.COLOR_BG))
    d = ImageDraw.Draw(img)

    nf = font("Bold", 26)
    tag = f"{idx:02d} / {total:02d}"
    tw = nf.getlength(tag) + 32
    d.rounded_rectangle((PAD, 70, PAD + tw, 116), radius=23, fill=(0, 0, 0))
    d.text((PAD + 16, 78), tag, font=nf, fill="#FFFFFF")

    draw_block(d, PAD, top, hlines, hf, hsize, 1.2, config.COLOR_TEXT, config.COLOR_ACCENT)
    y = top + hh + 44
    for b in blocks:
        draw_block(d, PAD, y, b, bf, size, 1.55, (232, 232, 232), config.COLOR_ACCENT)
        y += len(b) * size * 1.55 + gap

    d.text((PAD, 1272), config.BRAND_HANDLE, font=font("Bold", 26), fill=(200, 200, 200))
    _credit(d, credit, CARD_W - PAD, 1276)
    img.save(out, "JPEG", quality=94)


def _avatar(size: int) -> Image.Image | None:
    try:
        im = ImageOps.fit(Image.open(config.PROFILE_IMAGE).convert("RGB"), (size, size), Image.LANCZOS)
    except Exception:
        return None
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4, size * 4), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask.resize((size, size), Image.LANCZOS))
    return out


def _center(d, y, text, f, fill):
    d.text(((CARD_W - f.getlength(text)) / 2, y), text, font=f, fill=fill)


def card_outro(photo: Path | None, share_target: str, out: Path):
    """마지막 장: 흐린 배경 + 프사 + 이름/핸들 + 'OO 친구에게 보내주세요' + 팔로우 버튼."""
    from PIL import ImageFilter
    if photo:
        bg = cover_fit(photo, CARD_W, CARD_H).filter(ImageFilter.GaussianBlur(14))
        bg = Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), 0.62)
    else:
        bg = fallback_bg(CARD_W, CARD_H)
    img = bg.convert("RGBA")
    d = ImageDraw.Draw(img)

    # 프사 (흰 테두리 원)
    size, cy = 230, 420
    ring = 9
    d.ellipse((CARD_W / 2 - size / 2 - ring, cy - size / 2 - ring, CARD_W / 2 + size / 2 + ring,
               cy + size / 2 + ring), fill=(255, 255, 255, 255))
    av = _avatar(size)
    if av:
        img.alpha_composite(av, (int(CARD_W / 2 - size / 2), int(cy - size / 2)))
    d = ImageDraw.Draw(img)

    _center(d, cy + size / 2 + 34, config.BRAND_DISPLAY, font("Bold", 38), (255, 255, 255, 255))
    _center(d, cy + size / 2 + 86, config.BRAND_HANDLE, font("Regular", 32), (190, 190, 190, 255))

    target = (share_target or "이 소식 궁금한").strip()
    headline = f"{target} 친구에게\n**보내주세요**"
    hf, lines, hs, hh = fit_text(headline, "Black", 78, 52, CARD_W - 120, 220, lh=1.28)
    hy = 760
    draw_block(d, 60, hy, lines, hf, hs, 1.28, "#FFFFFF", config.COLOR_ACCENT, align="center", box_w=CARD_W - 120)

    y = hy + hh + 34
    _center(d, y, config.OUTRO_SUB, font("Medium", 38), (205, 205, 205, 255))

    bw, bh = 470, 108
    by = y + 100
    d.rounded_rectangle(((CARD_W - bw) / 2, by, (CARD_W + bw) / 2, by + bh), radius=bh / 2, fill="#FFFFFF")
    bf = font("Bold", 44)
    label = "+ 팔로우"
    tb = bf.getbbox(label)
    d.text(((CARD_W - bf.getlength(label)) / 2, by + (bh - (tb[3] - tb[1])) / 2 - tb[1]), label,
           font=bf, fill="#111111")

    _center(d, by + bh + 34, config.OUTRO_FOOT, font("Regular", 30), (185, 185, 185, 255))
    img.convert("RGB").save(out, "JPEG", quality=94)


# ---------- 릴스 ----------

def reel_background(label: str, title: str, out: Path) -> int:
    """고정 레이어(라벨, 제목, 브랜드). 영상이 들어갈 y 위치를 반환."""
    img = Image.new("RGB", (REEL_W, REEL_H), rgb(config.COLOR_BG))
    d = ImageDraw.Draw(img)

    y = 250
    if label:
        lf = font("Bold", 40)
        lw = lf.getlength(label) + 40
        lx = (REEL_W - lw) / 2
        d.rectangle((lx, y, lx + lw, y + 64), fill="#FFFFFF")
        d.text((lx + 20, y + 8), label, font=lf, fill="#000000")
        y += 64 + 34

    box_w = REEL_W - 100
    tf, lines, size, th = fit_text(title, "Black", 86, 56, box_w, 260, lh=1.24)
    draw_block(d, 50, y, lines, tf, size, 1.24, config.COLOR_TEXT, config.COLOR_ACCENT,
               align="center", box_w=box_w, hl_mode="box")
    media_y = int(y + th + 44)

    by = media_y + REEL_MEDIA_H + 46
    bf = font("Black", 68)
    d.text(((REEL_W - bf.getlength(config.BRAND_NAME)) / 2, by), config.BRAND_NAME,
           font=bf, fill=config.COLOR_ACCENT)
    if config.REEL_SUBLINE:
        sf = font("Bold", 34)
        d.text(((REEL_W - sf.getlength(config.REEL_SUBLINE)) / 2, by + 96), config.REEL_SUBLINE,
               font=sf, fill="#FFFFFF")
    img.save(out, "PNG")
    return media_y


def reel_overlay(subtitle: str, credit: str, media_y: int, out: Path):
    """장면별 레이어(영상 위 자막 박스, 출처). 투명 PNG."""
    img = Image.new("RGBA", (REEL_W, REEL_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if subtitle:
        f, lines, size, h = fit_text(subtitle, "Bold", 46, 32, REEL_W - 200, 180, lh=1.3)
        bottom = media_y + REEL_MEDIA_H - 50
        top = bottom - h - 24
        widest = max(f.getlength(_s(l)) for l in lines)
        x0 = (REEL_W - widest) / 2 - 22
        d.rectangle((x0, top, REEL_W - x0, bottom), fill=(0, 0, 0, 225))
        draw_block(d, 100, top + 10, lines, f, size, 1.3, "#FFFFFF", config.COLOR_ACCENT,
                   align="center", box_w=REEL_W - 200)
    if credit:
        cf = font("Regular", 22)
        credit = credit[:60]
        d.text((REEL_W - 40 - cf.getlength(credit), media_y + REEL_MEDIA_H + 8), credit,
               font=cf, fill=(150, 150, 150, 255))
    img.save(out, "PNG")


def reel_fallback_panel(out: Path):
    fallback_bg(REEL_W, REEL_MEDIA_H).save(out, "JPEG", quality=92)
