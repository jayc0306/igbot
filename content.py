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
- 원문과 web_search로 확인한 사실만 쓴다. 확인 안 된 건 아예 언급하지 않는다.
- 원문이 짧거나 맥락이 부족하면 web_search로 이 사건의 배경을 1~3번 찾아서 맥락을 채운다 (언제, 어디서, 왜 화제인지).
- 아래 같은 면책, 당부, 훈계성 문장은 절대 쓰지 않는다:
  "아직 확인되지 않았다", "공식 발표를 기다려야 한다", "지켜봐야 한다", "주의가 필요하다",
  "추측은 자제하자", "정확한 내용은 추후 확인", "~인 만큼 신중해야 한다"
  확인된 사실만 나열하고 거기서 끝낸다.
- 마침표(.)를 쓰지 않는다. 한 줄에 한 문장, 문장 끝은 마침표 없이 끝낸다. (말줄임표 ... 는 허용)
- 주제만 주어지면 일반적으로 확인 가능한 내용 위주. 확실하지 않은 구체 수치는 넣지 않는다.
- em dash(—) 쓰지 않는다. 이미지에 들어갈 텍스트에 이모지 금지 (캡션에는 1~3개 허용).
- 핵심 단어 1개를 **이렇게** 감싸면 이미지에서 강조색으로 표시된다. 제목당 최대 1개.
- image_query 는 스톡 사진 검색용 영어 키워드 2~4단어 (예: "paris protest police").
- 출력은 JSON 하나만. 코드블록, 설명 없이.

캡션 형식 (레퍼런스: today.worthy 같은 이슈 계정의 이야기체)
- 존댓말 합니다체. 기자 보고서체(~했다, ~이다)나 딱딱한 나열 금지
- 첫 줄: [제목을 문장으로] 예: [신입사원이 회장님 아들이 됐습니다] [루이비통 매장만 멀쩡했습니다]
- 빈 줄 후 본문 3~4줄. 첫 줄은 🚨 같은 이모지 하나로 시작해도 좋다. 군더더기 없이 핵심만
- 사진/영상을 묘사하지 말고 "무슨 일인지"를 이야기한다
  나쁜 예: "매장 앞에 사람들이 몰려 있는 영상이 올라왔다", "조명도 멀쩡해 보인다", "게시자는 ~라고 평했다"
  좋은 예: "폭동이 이어지는 파리에서 루이비통 매장 영상이 퍼지고 있습니다"
          "주변 상점들이 털리는 와중에도 이 매장만 불이 켜진 채 그대로였습니다"
- 흐름: 무슨 일인지 → 왜 화제인지(배경/맥락) → 핵심 숫자나 디테일 → 반응이나 의미
- 문장끼리 자연스럽게 이어지게. 앞 문장을 받아서 다음 문장이 나오도록
- 마지막 줄: 저장/공유 유도 한 줄 (예: 저장해두고 친구한테도 공유해주세요). 해시태그는 넣지 않는다
- 마침표 없이, 한 줄에 한 문장. 캡션에 @아이디 쓰지 않는다
""".strip()

CARD_SPEC = """
카드뉴스 JSON (레퍼런스: today.worthy):
{
  "cover_sub": "커버 첫 줄. 작은 글씨로 상황 설명, 18자 이내 (예: 22년 전 신입사원으로 들어간 회사)",
  "cover_title": "커버 둘째 줄. 큰 글씨 한 방, 14자 이내, 핵심 단어 1개 **강조** (예: 회장님 **아들**이 됐다)",
  "slides": [
    {"kicker": "작은 라벨 12자 이내, 필요 없으면 빈 문자열 (예: 신입사원에서 아들까지)",
     "heading": "소제목. 2줄 이내(\\n), 줄당 14자 이내, 강조 1개 가능 (예: 주식 **10%**\\n이미 넘겨받았다)",
     "body": "1~2문장, 문장마다 \\n, 전체 60자 이내. 소제목을 반복하지 말고 꼭 필요한 사실/숫자 하나만",
     "image_query": "이 장에 쓸 스톡 사진 영어 키워드"}
  ],
  "share_target": "마지막 장 '___ 친구에게 보내주세요'에 들어갈 말, 위트 있게 14자 이내 (예: 회장님한테 입양 가고 싶은, 프랑스 사태 관심 있는)",
  "caption": "...",
  "hashtags": ["#...", "3~5개, 검색 많이 되는 핵심 키워드만"]
}
글자는 최소로. 사진이 주인공이고 글은 거드는 역할. 같은 말 반복, 뻔한 수식어, 군더더기 금지.
slides 개수는 아래 지시를 정확히 따른다 (0장일 수도 있다).
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


SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}


def _final_text(blocks) -> str:
    """검색을 했으면 마지막 검색 결과 뒤에 나온 글만 (JSON 앞의 '찾아볼게요' 같은 말 제외)."""
    last_tool = -1
    for i, b in enumerate(blocks):
        if b.type in ("server_tool_use", "web_search_tool_result"):
            last_tool = i
    return "".join(b.text for b in blocks[last_tool + 1:] if b.type == "text")


def _call(system, messages, max_tokens, search):
    kwargs = dict(model=config.CLAUDE_MODEL, max_tokens=max_tokens, system=system, messages=messages)
    if search:
        kwargs["tools"] = [SEARCH_TOOL]
    msg = client.messages.create(**kwargs)
    turns = 0
    while getattr(msg, "stop_reason", "") == "pause_turn" and turns < 3:  # 검색이 길어지면 이어서
        kwargs["messages"] = messages + [{"role": "assistant", "content": msg.content}]
        msg = client.messages.create(**kwargs)
        turns += 1
    return msg


def _ask(system: str, user_content, max_tokens=4000, search=False) -> dict:
    messages = [{"role": "user", "content": user_content}]
    for attempt in range(2):
        try:
            msg = _call(system, messages, max_tokens, search)
        except anthropic.BadRequestError:
            if not search:
                raise
            search = False  # 검색 도구를 못 쓰는 환경이면 검색 없이
            msg = _call(system, messages, max_tokens, False)
        text = _final_text(msg.content)
        try:
            return _parse_json(text)
        except Exception:
            # JSON이 아니면 한 번 더: 소스가 짧아도 그걸로 만들라고 다시 요청
            messages += [{"role": "assistant", "content": text or "(빈 응답)"},
                         {"role": "user", "content": "설명이나 질문 없이, 주어진 소스만으로 최선을 다해 "
                                                     "스키마에 맞는 JSON 하나만 출력해."}]
            search = False
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
    data = _ask(COMMON_RULES + "\n\n" + spec, content, max_tokens=6000, search=config.CONTEXT_SEARCH)
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


EDIT_SPEC = """
너는 인스타 카드뉴스/릴스 봇의 '수정 요청 해석기'다.
사용자가 방금 만든 결과물을 고치려고 보낸 메시지를 읽고, 무엇을 어떻게 바꿀지 JSON 하나로만 답한다.

{
  "title": "새 제목 (영상/커버 큰 제목). 바꾸라는 말이 없으면 null",
  "sub": "새 작은 문구 (카드뉴스: 커버 위 작은 첫 줄 / 릴스: 제목 위 작은 라벨). 없으면 null",
  "source": "새 출처 표기 (예: X, 연합뉴스). 출처를 빼라고 하면 빈 문자열. 없으면 null",
  "caption": "캡션 전체를 이걸로 바꾸라고 문장을 통째로 줬을 때만 그 캡션. 아니면 null",
  "rewrite": "위 항목으로 안 되는 나머지 수정 요청을 한 문장으로 (예: 2번 장 숫자 빼기, 제목을 더 자극적으로). 없으면 null"
}

규칙
- 사용자가 원하는 문구를 직접 줬으면 글자 하나 바꾸지 말고 그대로 넣는다 (물음표, 느낌표, 띄어쓰기 포함).
- "이걸로 바꿔", "이걸로 하라고", "위에꺼로" 처럼 가리키면 메시지 안에 있는 그 문구를 찾아서 넣는다.
- 지시어나 감정 표현(바꾸라고, 해줘, ;;, ㅋㅋ, 시발 등)은 값에 절대 넣지 않는다.
- 오타가 있어도 의도를 읽는다 (예: "ㅈ ㅔ목을" = 제목을).
- 제목을 어떻게 바꿀지 문구 없이 방향만 말하면 (예: 제목 더 세게) title은 null, rewrite에 적는다.

예시
메시지: "여자 때리는 법 가르치는 이슬람 모스크?\n\n제목이걸로 바꾸라고;;"
→ {"title": "여자 때리는 법 가르치는 이슬람 모스크?", "sub": null, "source": null, "caption": null, "rewrite": null}
메시지: "출처 X로 하고 2번 장 숫자 좀 빼줘"
→ {"title": null, "sub": null, "source": "X", "caption": null, "rewrite": "2번 장의 숫자 빼기"}
""".strip()


def parse_edit_request(text: str, state: dict) -> dict:
    user = (f"지금 결과물 상태:\n{json.dumps(state, ensure_ascii=False)}\n\n"
            f"사용자 메시지:\n<message>\n{text}\n</message>")
    d = _ask(EDIT_SPEC, user, max_tokens=800)
    return {k: d.get(k) for k in ("title", "sub", "source", "caption", "rewrite")}


INLINE_RE = re.compile(r"^\s*(제목|출처|캡션|라벨|부제)\s*[:：]\s*(.+?)\s*$")


def extract_inline_overrides(text: str) -> tuple[str, dict]:
    """소스와 같이 보낸 '제목: ...', '출처: ...' 줄을 지시로 떼어냄."""
    keymap = {"제목": "title", "출처": "source", "캡션": "caption", "라벨": "sub", "부제": "sub"}
    kept, ov = [], {}
    for line in (text or "").split("\n"):
        m = INLINE_RE.match(line)
        if m:
            ov[keymap[m.group(1)]] = m.group(2)
        else:
            kept.append(line)
    return "\n".join(kept).strip(), ov


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
