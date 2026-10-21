"""Claude로 원고 생성 + 자동 수집한 메시지 중 이슈 고르기."""
import base64
import io
import json
import re
from pathlib import Path

import anthropic
import httpx
from PIL import Image

import config

client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
URL_RE = re.compile(r"https?://\S+")

COMMON_RULES = f"""
너는 인스타그램 이슈 매거진 계정의 에디터다.
계정 주제: {config.ACCOUNT_TOPIC}
타깃 독자: {config.ACCOUNT_AUDIENCE}
브랜드: {config.BRAND_NAME} ({config.BRAND_HANDLE})

톤 (레퍼런스: 이슈 매거진 계정들)
- 제목은 짧고 세게. 스크롤을 멈추게 하는 한 방. 예시 톤:
  "명품 매장들 탈탈 털리더니... 프랑스 길거리 근황", "현재 심각한 프랑스 시위 상태",
  "후쿠오카 우동 맛집 여기만 저장하세요!", "영화보다 영화 같은 삶"
- 자극적이어도 사실을 비틀지 않는다. 원문에 없는 숫자, 인용, 사건을 지어내지 않는다.
- 원문에 없는 건 아예 언급하지 않는다. 아래 같은 면책, 당부, 훈계성 문장은 절대 쓰지 않는다:
  "아직 확인되지 않았다", "공식 발표를 기다려야 한다", "지켜봐야 한다", "주의가 필요하다",
  "추측은 자제하자", "정확한 내용은 추후 확인", "~인 만큼 신중해야 한다"
  확인된 사실만 나열하고 거기서 끝낸다.
- 마침표(.)를 쓰지 않는다. 한 줄에 한 문장, 문장 끝은 마침표 없이 끝낸다. (말줄임표 ... 는 허용)
- 주제만 주어지면 일반적으로 확인 가능한 내용 위주. 확실하지 않은 구체 수치는 넣지 않는다.
- em dash(—) 쓰지 않는다. 이미지에 들어갈 텍스트에 이모지 금지 (캡션에는 1~3개 허용).
- 핵심 단어 1개를 **이렇게** 감싸면 이미지에서 강조색으로 표시된다. 제목당 최대 1개.
- image_query 는 스톡 사진 검색용 영어 키워드 2~4단어 (예: "paris protest police").
- 출력은 JSON 하나만. 코드블록, 설명 없이.

캡션 형식
- 첫 줄: [제목 한 줄]
- 빈 줄 후 본문 4~7줄. 무슨 일인지, 배경, 숫자, 반응 순. 한 줄에 한 문장.
- 마지막 줄: 저장이나 팔로우 유도 한 줄 (예: "저장해두고 친구한테도 공유해주세요"). 해시태그는 caption에 넣지 않는다.
- 캡션도 마침표 없이, 한 줄에 한 문장.
- 캡션에 @아이디(계정 태그)를 쓰지 않는다.
""".strip()

CARD_SPEC = """
카드뉴스 JSON (레퍼런스: today.worthy):
{
  "cover_sub": "커버 첫 줄. 작은 글씨로 상황 설명, 22자 이내 (예: 22년 전 신입사원으로 들어간 회사)",
  "cover_title": "커버 둘째 줄. 큰 글씨 한 방, 14자 이내, 핵심 단어 1개 **강조** (예: 회장님 **아들**이 됐다)",
  "slides": [
    {"kicker": "작은 라벨 14자 이내 (예: 신입사원에서 아들까지)",
     "heading": "소제목. 2줄 이내(\\n), 줄당 14자 이내, 강조 1개 가능 (예: 주식 **10%**\\n이미 넘겨받았다)",
     "body": "2~3문장, 문장마다 \\n, 전체 90자 이내. 구체적인 사실과 숫자",
     "image_query": "이 장에 쓸 스톡 사진 영어 키워드"}
  ],
  "share_target": "마지막 장 '___ 친구에게 보내주세요'에 들어갈 말, 위트 있게 14자 이내 (예: 회장님한테 입양 가고 싶은, 프랑스 사태 관심 있는)",
  "caption": "...",
  "hashtags": ["#...", "3~5개, 검색 많이 되는 핵심 키워드만"]
}
흐름: 무슨 일 → 배경 → 핵심 숫자/장면 → 반응/영향 → 한 줄 정리.
slides 개수는 아래 지시를 정확히 따른다.
""".strip()

REEL_SPEC = """
릴스 JSON:
{
  "label": "상단 작은 라벨. 장소나 분류, 8자 이내 (예: 프랑스 파리)",
  "title": "영상 내내 상단에 고정되는 제목. \\n 으로 2줄, 줄당 13자 이내",
  "scenes": [
    {"subtitle": "영상 위 자막 한 줄, 20자 이내", "narration": "이 장면에서 읽을 1~2문장, 구어체", "image_query": "영어 키워드"}
  ],
  "caption": "...",
  "hashtags": ["#...", "3~5개, 검색 많이 되는 핵심 키워드만"]
}
scenes 4~6개. 내레이션 전체 20~35초. 첫 장면 자막은 상황 설명 한 줄, 마지막은 한 줄 결론.
""".strip()

REEL_SPEC_SIMPLE = """
릴스 JSON (영상은 원본 그대로 나가고, 위에 제목만 고정으로 붙는다):
{
  "label": "상단 작은 라벨. 장소나 분류, 8자 이내 (예: 프랑스 파리)",
  "title": "영상 위에 고정되는 제목. \\n 으로 2줄, 줄당 13자 이내. 영상을 보고 싶게 만드는 한 방",
  "image_query": "첨부 영상이 없을 때 스톡 영상 검색용 영어 키워드",
  "caption": "...",
  "hashtags": ["#...", "3~5개, 검색 많이 되는 핵심 키워드만"]
}
""".strip()

SOCIAL_HOSTS = ("x.com/", "twitter.com/", "instagram.com/", "youtube.com/shorts", "youtu.be/",
                "tiktok.com/")

SELECT_SPEC = """
너는 인스타 이슈 매거진의 데스크다. 아래는 여러 텔레그램 채널에서 모은 최근 메시지 목록이다.
오늘 올릴 이슈를 최대 {n}개 골라라.

기준
- 계정 주제와 타깃에 맞고, 저장/공유가 잘 될 만한 것
- 광고, 홍보, 레퍼럴, 단순 시세 알림, 내용 없는 짧은 글 제외
- 같은 사건을 다룬 메시지는 하나로 묶는다 (message_ids 에 모두)
- 최근에 이미 올린 주제와 겹치면 제외: {recent}
- 영상이 붙은 메시지가 핵심이면 kind=reel, 아니면 card

JSON만 출력:
{{"issues": [{{"title": "가제", "kind": "card|reel", "message_ids": [번호...], "why": "한 줄 이유"}}]}}
고를 게 없으면 {{"issues": []}}
""".strip()


OG_RE = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*>', re.I)
CONTENT_RE = re.compile(r'content=["\']([^"\']+)["\']', re.I)


def fetch_page(url: str, limit: int = 6000) -> tuple[str, str]:
    """기사 본문 텍스트와 대표 이미지(og:image) 주소."""
    try:
        import trafilatura

        r = httpx.get(url, timeout=15, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124"})
        text = (trafilatura.extract(r.text) or "")[:limit]
        img = ""
        for tag in OG_RE.findall(r.text[:200000]):
            m = CONTENT_RE.search(tag)
            if m:
                img = httpx.URL(str(r.url)).join(m.group(1).replace("&amp;", "&")).__str__()
                break
        return text, img
    except Exception:
        return "", ""


def build_source(raw: str, extra_urls: list[str] | None = None) -> tuple[str, list[tuple[str, str]], list[str]]:
    """returns (원문 텍스트, [(이미지 주소, 출처 도메인)], [본문을 읽어온 사이트 도메인])"""
    raw = raw.strip()
    parts = [raw]
    urls = list(dict.fromkeys([u.rstrip(").,]") for u in URL_RE.findall(raw)] + (extra_urls or [])))
    urls = [u for u in urls if "t.me/" not in u and not any(h in u for h in SOCIAL_HOSTS)]
    images, domains = [], []
    for u in urls[:2]:
        body, img = fetch_page(u)
        host = httpx.URL(u).host.replace("www.", "")
        if body:
            parts.append(f"[링크 본문: {u}]\n{body}")
            domains.append(host)
        if img:
            images.append((img, host))
    return "\n\n".join(parts), images, list(dict.fromkeys(domains))


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def _image_block(path: Path) -> dict:
    img = Image.open(path).convert("RGB")
    img.thumbnail((1024, 1024))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


def _ask(system: str, user_content, max_tokens=4000) -> dict:
    messages = [{"role": "user", "content": user_content}]
    for attempt in range(2):
        msg = client.messages.create(model=config.CLAUDE_MODEL, max_tokens=max_tokens, system=system,
                                     messages=messages)
        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            return _parse_json(text)
        except Exception:
            # JSON이 아니면 한 번 더: 소스가 짧아도 그걸로 만들라고 다시 요청
            messages += [{"role": "assistant", "content": text or "(빈 응답)"},
                         {"role": "user", "content": "설명이나 질문 없이, 주어진 소스만으로 최선을 다해 "
                                                     "스키마에 맞는 JSON 하나만 출력해."}]
    raise ValueError("원고를 못 만들었어요. 소스가 너무 짧거나 애매하면 내용을 조금 더 붙여서 보내주세요.")


_SENT_SPLIT = re.compile(r"(?<=[가-힣a-zA-Z0-9)\]'\"])(?<!\.)\.\s+(?=\S)")
_TRAIL_DOT = re.compile(r"(?<!\.)\.\s*$")


def clean(text: str) -> str:
    """마침표 제거: 문장 중간 마침표는 줄바꿈으로, 줄 끝 마침표는 삭제 (말줄임표는 유지)."""
    if not isinstance(text, str):
        return text
    text = text.replace("\\n", "\n")
    lines = []
    for line in text.split("\n"):
        line = _SENT_SPLIT.sub("\n", line)
        lines += [_TRAIL_DOT.sub("", l).rstrip() for l in line.split("\n")]
    return "\n".join(lines)


def _clean_data(kind: str, d: dict) -> dict:
    keys = ["cover_sub", "cover_title", "caption", "share_target"] if kind == "card" else ["label", "title", "caption"]
    for k in keys:
        if k in d:
            d[k] = clean(d[k])
    for s in d.get("slides", []):
        s["kicker"] = clean(s.get("kicker", ""))
        s["heading"] = clean(s.get("heading", ""))
        s["body"] = clean(s.get("body", ""))
    for s in d.get("scenes", []):
        s["subtitle"] = clean(s.get("subtitle", ""))
    return d


def generate(kind: str, source: str, media_note: str = "", photos: list[Path] | None = None,
             feedback: str | None = None, previous: dict | None = None) -> dict:
    spec = CARD_SPEC if kind == "card" else (REEL_SPEC if config.REEL_SUBTITLES else REEL_SPEC_SIMPLE)
    text = f"아래 원문으로 {'카드뉴스' if kind == 'card' else '릴스'} 원고를 만들어.\n\n<source>\n{source}\n</source>"
    if media_note:
        text += f"\n\n{media_note}"
    if previous and feedback:
        text += (f"\n\n이전 원고:\n{json.dumps(previous, ensure_ascii=False)}"
                 f"\n\n수정 요청: {feedback}\n반영해서 전체 JSON을 다시 출력해.")
    content = [_image_block(p) for p in (photos or [])[:10]] + [{"type": "text", "text": text}]
    data = _ask(COMMON_RULES + "\n\n" + spec, content)
    if kind == "card":
        data["slides"] = data.get("slides", [])[:9]
    else:
        data["scenes"] = data.get("scenes", [])[:7]
    return _clean_data(kind, data)


def select_issues(messages: list[dict], recent_titles: list[str], n: int) -> list[dict]:
    lines = []
    for i, m in enumerate(messages):
        media = f" [첨부: {m['media_kind']}]" if m.get("media_kind") else ""
        lines.append(f"#{i} ({m['channel']}){media}\n{m['text'][:600]}")
    system = SELECT_SPEC.format(n=n, recent=", ".join(recent_titles[-20:]) or "없음")
    system += f"\n\n계정 주제: {config.ACCOUNT_TOPIC} / 타깃: {config.ACCOUNT_AUDIENCE}"
    data = _ask(system, "\n\n".join(lines), max_tokens=2000)
    return data.get("issues", [])[:n]


def build_caption(data: dict, sources: list[str] | None = None) -> str:
    # 고정 해시태그 먼저, 전체 최대 HASHTAG_LIMIT개
    raw = config.FIXED_HASHTAGS.split() + data.get("hashtags", [])
    tags = list(dict.fromkeys(t if t.startswith("#") else f"#{t}" for t in raw if t))[:config.HASHTAG_LIMIT]
    # 캡션에는 @아이디를 넣지 않음 (태그/알림 방지). "X @abc" → "X"
    caption = re.sub(r"\s*@[A-Za-z0-9_.]+", "", data.get("caption", "").replace("**", "")).strip()
    names = [re.sub(r"\s*@[A-Za-z0-9_.]+", "", s).strip() for s in (sources or [])]
    names = [n for n in dict.fromkeys(names) if n]
    if names:
        caption += "\n\n출처: " + ", ".join(names)
    return f"{caption}\n\n{' '.join(tags)}".strip()[:2200]
