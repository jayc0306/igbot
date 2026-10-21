"""지정한 텔레그램 채널에서 새 글 수집 (내 텔레그램 계정으로 읽기, Telethon)."""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient

import config
from media import Media

log = logging.getLogger("igbot.collector")
DATA_DIR = config.BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
SESSION = DATA_DIR / "tg_user"
STATE_FILE = DATA_DIR / "collector_state.json"
MAX_VIDEO_MB = 80


def _media_kind(m) -> str | None:
    if m.photo:
        return "photo"
    if m.video or m.gif:
        return "video"
    if m.document and (m.file.mime_type or "").startswith("image/"):
        return "photo"
    return None


class Collector:
    def __init__(self):
        self.client: TelegramClient | None = None
        self.ready = False
        try:
            self.state = json.loads(STATE_FILE.read_text())
        except Exception:
            self.state = {"last_ids": {}, "recent_titles": []}

    def _save(self):
        STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))

    async def start(self) -> bool:
        if not (config.TG_API_ID and config.TG_API_HASH and config.SOURCE_CHANNELS):
            log.info("채널 자동 수집 꺼짐 (TG_API_ID / TG_API_HASH / SOURCE_CHANNELS 미설정)")
            return False
        self.client = TelegramClient(str(SESSION), config.TG_API_ID, config.TG_API_HASH)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.warning("텔레그램 계정 로그인 필요: python login.py 먼저 실행")
            return False
        self.ready = True
        log.info(f"채널 자동 수집 준비 완료: {len(config.SOURCE_CHANNELS)}개 채널")
        return True

    async def stop(self):
        if self.client:
            await self.client.disconnect()

    async def fetch(self) -> list[dict]:
        """채널별 새 글. 앨범(여러 장 묶음)은 한 항목으로 합침."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=config.LOOKBACK_HOURS)
        entries: dict = {}
        for ch in config.SOURCE_CHANNELS:
            try:
                entity = await self.client.get_entity(ch)
            except Exception as e:
                log.warning(f"채널 못 찾음 {ch}: {e}")
                continue
            title = getattr(entity, "title", ch)
            username = getattr(entity, "username", None)
            last = self.state["last_ids"].get(ch, 0)
            max_id = last
            async for m in self.client.iter_messages(entity, limit=80, min_id=last):
                if m.date < cutoff:
                    break
                max_id = max(max_id, m.id)
                key = (ch, m.grouped_id or m.id)
                e = entries.setdefault(key, {"channel": title, "username": username, "msgs": [],
                                             "text": "", "media_kind": None, "id": m.id})
                e["msgs"].append(m)
                e["id"] = min(e["id"], m.id)
                if m.message:
                    e["text"] = (e["text"] + "\n" + m.message).strip()
                kind = _media_kind(m)
                if kind == "video" or (kind and not e["media_kind"]):
                    e["media_kind"] = kind
            self.state["last_ids"][ch] = max_id
        self._save()

        out = []
        for e in entries.values():
            if len(e["text"]) < 15 and not e["media_kind"]:
                continue
            e["link"] = f"https://t.me/{e['username']}/{e['id']}" if e["username"] else ""
            out.append(e)
        out.sort(key=lambda e: e["msgs"][0].date, reverse=True)
        return out[:120]

    async def download(self, entry: dict, dest: Path, max_items: int = 4) -> list[Media]:
        dest.mkdir(parents=True, exist_ok=True)
        credit = f"Visual reference: {entry['channel']}"
        result = []
        for m in sorted(entry["msgs"], key=lambda x: x.id):
            kind = _media_kind(m)
            if not kind:
                continue
            if kind == "video" and (m.file.size or 0) > MAX_VIDEO_MB * 1024 * 1024:
                continue
            try:
                path = await self.client.download_media(m, file=str(dest / f"tg_{m.id}"))
            except Exception as e:
                log.warning(f"미디어 다운로드 실패: {e}")
                continue
            if path:
                result.append(Media(Path(path), kind, credit))
            if len(result) >= max_items:
                break
        return result

    def remember(self, title: str):
        self.state["recent_titles"] = (self.state["recent_titles"] + [title])[-40:]
        self._save()

    @property
    def recent_titles(self) -> list[str]:
        return self.state["recent_titles"]
