"""텔레그램 → 인스타그램 카드뉴스/릴스 봇.

1) 수동: 주제/링크/포워드(사진·영상 포함) 전송 → [카드뉴스|릴스] → 미리보기 → 승인 → 게시
2) 자동: 정해둔 시간마다 지정 채널 새 글 수집 → 이슈 선별 → 생성 → 미리보기(또는 바로 게시)
"""
import asyncio
import html
import logging
import shutil
import uuid
from datetime import time as dtime
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import (InlineKeyboardButton as B, InlineKeyboardMarkup, InputMediaDocument,
                      InputMediaPhoto, InputMediaVideo, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import config
import content
import pipeline
from collector import Collector
from media import Media

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("igbot")
logging.getLogger("httpx").setLevel(logging.WARNING)

MANUAL = config.UPLOAD_MODE != "api"  # manual: 파일만 받아서 직접 업로드
_ig = None


def get_ig():
    global _ig
    if _ig is None:
        from instagram import Instagram
        _ig = Instagram()
    return _ig


collector = Collector()
JOBS: dict[str, dict] = {}
LABEL = {"card": "카드뉴스", "reel": "릴스"}
BOT_FILE_LIMIT = 20 * 1024 * 1024  # 텔레그램 봇이 받을 수 있는 파일 한도


# ---------- 공통 ----------

def authorized(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid not in config.ALLOWED_USER_IDS:
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"권한 없음. 내 user id: {uid}\n.env의 ALLOWED_USER_IDS에 넣고 봇을 다시 켜세요.")
            return
        return await fn(update, ctx)
    return wrapper


def kb_format(jid):
    return InlineKeyboardMarkup([
        [B("📰 카드뉴스", callback_data=f"fmt|card|{jid}"), B("🎬 릴스", callback_data=f"fmt|reel|{jid}")],
        [B("❌ 취소", callback_data=f"x|{jid}")],
    ])


def kb_review(jid, kind):
    other = "reel" if kind == "card" else "card"
    if MANUAL:
        return InlineKeyboardMarkup([
            [B("📎 원본 파일로 받기", callback_data=f"file|{jid}"), B("🏷 출처 수정", callback_data=f"src|{jid}")],
            [B("✏️ 수정 요청", callback_data=f"fb|{jid}"), B("🔄 다시 생성", callback_data=f"re|{jid}")],
            [B(f"🔀 {LABEL[other]}로 만들기", callback_data=f"sw|{jid}"), B("✅ 완료", callback_data=f"done|{jid}")],
        ])
    return InlineKeyboardMarkup([
        [B("✅ 인스타 업로드", callback_data=f"ok|{jid}")],
        [B("✏️ 수정 요청", callback_data=f"fb|{jid}"), B("🔄 다시 생성", callback_data=f"re|{jid}")],
        [B(f"🔀 {LABEL[other]}로 만들기", callback_data=f"sw|{jid}"), B("❌ 취소", callback_data=f"x|{jid}")],
    ])


def origin_name(msg) -> str:
    origin = getattr(msg, "forward_origin", None)
    if origin is None:
        return ""
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    user = getattr(origin, "sender_user", None)
    return (getattr(chat, "title", None) or getattr(origin, "sender_user_name", None)
            or getattr(user, "full_name", None) or "")


def extract_urls(msg) -> list[str]:
    """본문에 보이는 링크 + 글자에 숨은 하이퍼링크 + 링크 미리보기 주소."""
    urls = []
    for ents, parse in ((msg.entities, msg.parse_entity), (msg.caption_entities, msg.parse_caption_entity)):
        for e in ents or ():
            if e.type == "text_link" and e.url:
                urls.append(e.url)
            elif e.type == "url":
                urls.append(parse(e).strip())
    lp = getattr(msg, "link_preview_options", None)
    if lp and getattr(lp, "url", None):
        urls.append(lp.url)
    return [u for u in dict.fromkeys(urls) if "t.me/" not in u]


def new_job(chat_id: int, raw: str = "", kind=None) -> str:
    jid = uuid.uuid4().hex[:8]
    d = config.WORK_DIR / jid
    (d / "src").mkdir(parents=True, exist_ok=True)
    JOBS[jid] = {"raw": raw, "kind": kind, "chat_id": chat_id, "dir": d, "source": None,
                 "media": [], "sources": [], "urls": []}
    return jid


def cleanup(jid):
    job = JOBS.pop(jid, None)
    if job:
        shutil.rmtree(job["dir"], ignore_errors=True)


async def download_media(msg, dest: Path, credit: str) -> Media | None:
    """메시지에 붙은 사진/영상을 받아옴. 20MB 넘는 영상은 텔레그램 봇 제한으로 못 받음."""
    obj, kind, ext = None, None, ""
    if msg.photo:
        obj, kind, ext = msg.photo[-1], "photo", ".jpg"
    elif msg.video:
        obj, kind, ext = msg.video, "video", ".mp4"
    elif msg.animation:
        obj, kind, ext = msg.animation, "video", ".mp4"
    elif msg.document and (msg.document.mime_type or "").startswith(("video/", "image/")):
        obj = msg.document
        kind = "video" if obj.mime_type.startswith("video/") else "photo"
        ext = Path(obj.file_name or "").suffix or (".mp4" if kind == "video" else ".jpg")
    if not obj:
        return None
    if (obj.file_size or 0) > BOT_FILE_LIMIT:
        raise ValueError("파일이 20MB를 넘어서 봇이 받을 수 없어요 (텔레그램 봇 제한)")
    f = await obj.get_file()
    path = dest / f"{obj.file_unique_id}{ext}"
    await f.download_to_drive(path)
    return Media(path, kind, credit)


# ---------- 생성 / 미리보기 / 업로드 ----------

async def produce(ctx, jid, feedback=None, rerender=False):
    job = JOBS[jid]
    chat_id = job["chat_id"]
    status = await ctx.bot.send_message(chat_id, "⏳ 준비 중…")

    async def progress(t):
        try:
            await status.edit_text(t)
        except Exception:
            pass

    try:
        await pipeline.make(job, feedback, progress, rerender)
        await status.delete()
    except Exception as e:
        log.exception("produce failed")
        reason = str(e)
        if isinstance(e, FileNotFoundError):
            reason = "필요한 프로그램(ffmpeg)을 못 찾았어요. setup.bat을 다시 실행해 주세요."
        await status.edit_text(f"⚠️ 생성 실패: {reason}",
                               reply_markup=InlineKeyboardMarkup([[B("🔄 다시 시도", callback_data=f"re|{jid}")]]))
        return

    if config.AUTO_PUBLISH and not MANUAL:
        await publish(ctx, jid)
    else:
        await preview(ctx, jid)


async def preview(ctx, jid):
    job = JOBS[jid]
    chat_id = job["chat_id"]
    try:
        if job["kind"] == "card":
            group = [InputMediaVideo(p.read_bytes(), supports_streaming=True, width=1080, height=1350)
                     if p.suffix == ".mp4" else InputMediaPhoto(p.read_bytes()) for p in job["files"]]
            await ctx.bot.send_media_group(chat_id, group)
        else:
            await ctx.bot.send_video(chat_id, job["files"][0].read_bytes(), supports_streaming=True,
                                     width=1080, height=1920)
    except Exception as e:
        log.exception("preview send failed")
        mb = job["files"][0].stat().st_size / 1024 / 1024
        await ctx.bot.send_message(chat_id, f"⚠️ 미리보기 전송 실패 ({mb:.0f}MB): {e}\n"
                                            "아래 '원본 파일로 받기'를 눌러보세요.")
    # 영상 전송이 실패해도 캡션과 버튼은 항상 보냄
    await send_caption(ctx, jid)


async def send_caption(ctx, jid):
    job = JOBS[jid]
    note = "📝 캡션 (박스 눌러서 복사)" if MANUAL else "📝 캡션"
    warns = job.get("warnings") or []
    if warns:
        note = "\n".join(f"⚠️ {html.escape(w)}" for w in dict.fromkeys(warns)) + "\n\n" + note
    await ctx.bot.send_message(job["chat_id"], f"{note}\n<pre>{html.escape(job['caption'])}</pre>",
                               parse_mode="HTML", reply_markup=kb_review(jid, job["kind"]))


async def send_files(ctx, jid):
    """화질 손실 없는 원본 파일로 전송 (탭 → 저장)."""
    job = JOBS[jid]
    files = job["files"] if job["kind"] == "card" else job["files"][:1]
    try:
        if len(files) == 1:
            await ctx.bot.send_document(job["chat_id"], files[0].read_bytes(), filename=files[0].name)
            return
        media = [InputMediaDocument(p.read_bytes(), filename=p.name) for p in files]
        await ctx.bot.send_media_group(job["chat_id"], media)
    except Exception as e:
        log.exception("file send failed")
        await ctx.bot.send_message(job["chat_id"], f"⚠️ 파일 전송 실패: {e}\n"
                                                   f"PC 폴더에서 직접 가져가세요: {files[0].parent}")


async def publish(ctx, jid):
    job = JOBS.get(jid)
    if not job or job.get("publishing"):
        return
    job["publishing"] = True
    chat_id = job["chat_id"]
    status = await ctx.bot.send_message(chat_id, "📤 인스타그램에 올리는 중…")
    keys = []
    import storage
    ig = get_ig()
    try:
        urls = []
        for p in job["files"]:
            key = f"igbot/{jid}/{p.name}"
            urls.append(await asyncio.to_thread(storage.upload, p, key))
            keys.append(key)
        if job["kind"] == "card":
            res = await asyncio.to_thread(ig.publish_carousel, urls, job["caption"])
        else:
            await status.edit_text("📤 인스타 쪽 영상 처리 기다리는 중… (최대 몇 분)")
            res = await asyncio.to_thread(ig.publish_reel, urls[0], job["caption"], urls[1])
        await status.edit_text(f"✅ 게시 완료\n{res.get('permalink') or res['id']}")
        if job.get("issue_title"):
            collector.remember(job["issue_title"])
        cleanup(jid)
    except Exception as e:
        log.exception("publish failed")
        job["publishing"] = False
        await status.edit_text(f"⚠️ 업로드 실패: {e}",
                               reply_markup=InlineKeyboardMarkup([[B("🔁 업로드 재시도", callback_data=f"ok|{jid}")]]))
    finally:
        if keys and config.DELETE_AFTER_PUBLISH and jid not in JOBS:
            await asyncio.to_thread(storage.delete, keys)


# ---------- 채널 자동 수집 ----------

async def run_collect(ctx, chat_id: int, manual: bool = False):
    if not collector.ready:
        if manual:
            await ctx.bot.send_message(chat_id, "채널 자동 수집이 아직 설정 안 됐어요. README의 '채널 자동 수집' 참고.")
        return
    msgs = await collector.fetch()
    if not msgs:
        if manual:
            await ctx.bot.send_message(chat_id, "새 글이 없어요.")
        return
    issues = await asyncio.to_thread(content.select_issues, msgs, collector.recent_titles, config.ISSUES_PER_RUN)
    if not issues:
        if manual:
            await ctx.bot.send_message(chat_id, f"새 글 {len(msgs)}개 확인, 올릴 만한 이슈는 없었어요.")
        return

    for k, iss in enumerate(issues, start=1):
        ids = [int(str(i).lstrip("#")) for i in iss.get("message_ids", []) if str(i).lstrip("#").isdigit()]
        entries = [msgs[i] for i in ids if 0 <= i < len(msgs)]
        if not entries:
            continue
        raw = "\n\n".join(f"[{e['channel']}] {e['text']}\n{e['link']}".strip() for e in entries)
        kind = iss.get("kind") if iss.get("kind") in ("card", "reel") else "card"
        jid = new_job(chat_id, raw, kind)
        job = JOBS[jid]
        job["sources"] = [e["channel"] for e in entries]
        job["issue_title"] = iss.get("title", "")
        if config.MEDIA_FROM_SOURCE:
            for e in entries:
                job["media"] += await collector.download(e, job["dir"] / "src")
        await ctx.bot.send_message(chat_id, f"🗞 자동 이슈 {k}/{len(issues)}: {iss.get('title', '')}\n"
                                            f"({iss.get('why', '')})")
        await produce(ctx, jid)


async def scheduled_collect(ctx: ContextTypes.DEFAULT_TYPE):
    await run_collect(ctx, config.NOTIFY_CHAT_ID)


# ---------- 핸들러 ----------

@authorized
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["주제, 링크, 다른 채널 메시지(사진·영상 포함)를 보내주세요."]
    if MANUAL:
        lines.append("카드뉴스/릴스를 고르면 만들어서 보내드려요. 저장해서 인스타에 직접 올리면 됩니다.")
    else:
        lines.append("카드뉴스/릴스를 고르면 만들어서 미리보기 → 승인하면 인스타에 올립니다.")
    lines += ["", "/card 주제  → 바로 카드뉴스", "/reel 주제  → 바로 릴스"]
    if not MANUAL:
        lines.append("/limit  → 오늘 게시 한도")
    if collector.ready:
        lines.append(f"/collect  → 채널 수집 ({len(config.SOURCE_CHANNELS)}개 채널, {', '.join(config.SCHEDULE_TIMES)})")
    await update.message.reply_text("\n".join(lines))


@authorized
async def cmd_direct(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kind = "card" if update.message.text.startswith("/card") else "reel"
    raw = " ".join(ctx.args).strip()
    reply = update.message.reply_to_message
    jid = new_job(update.effective_chat.id, raw, kind)
    if reply:
        name = origin_name(reply)
        JOBS[jid]["raw"] = (raw + "\n" + (reply.text or reply.caption or "")).strip()
        JOBS[jid]["urls"] += extract_urls(reply)
        try:
            m = await download_media(reply, JOBS[jid]["dir"] / "src", f"Visual reference: {name}" if name else "")
            if m:
                JOBS[jid]["media"].append(m)
        except ValueError as e:
            await update.message.reply_text(str(e))
        if name:
            JOBS[jid]["sources"].append(name)
    if not JOBS[jid]["raw"] and not JOBS[jid]["media"]:
        cleanup(jid)
        # 그냥 /reel 만 보냈으면 바로 전에 보낸 소스로 만듦
        last = ctx.user_data.get("last_job")
        if last in JOBS and not JOBS[last].get("files"):
            JOBS[last]["kind"] = kind
            await produce(ctx, last)
            return
        await update.message.reply_text(f"먼저 소스(주제, 링크, 포워드)를 보내고 버튼으로 고르거나, /{kind} 주제 형식으로 보내주세요.")
        return
    await produce(ctx, jid)


@authorized
async def cmd_collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 채널 새 글 확인 중…")
    await run_collect(ctx, update.effective_chat.id, manual=True)


@authorized
async def cmd_limit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        res = await asyncio.to_thread(get_ig().publishing_limit)
        await update.message.reply_text(f"게시 한도: {res}")
    except Exception as e:
        await update.message.reply_text(f"조회 실패: {e}")


async def flush_album(ctx: ContextTypes.DEFAULT_TYPE):
    jid = ctx.job.data
    if jid in JOBS:
        n = len(JOBS[jid]["media"])
        await ctx.bot.send_message(JOBS[jid]["chat_id"], f"사진/영상 {n}개 받았어요. 어떤 형식으로 만들까요?",
                                   reply_markup=kb_format(jid))


@authorized
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or msg.caption or "").strip()
    name = origin_name(msg)
    chat_id = update.effective_chat.id

    # 출처 수정 대기 중
    src_jid = ctx.user_data.pop("source_job", None)
    if src_jid and src_jid in JOBS and text:
        val = text.removeprefix("출처:").strip()
        JOBS[src_jid]["source_override"] = "" if val in ("없음", "없애", "삭제", "-") else val
        await produce(ctx, src_jid, rerender=True)
        return

    # 수정 요청 대기 중이면 피드백으로 처리
    fb_jid = ctx.user_data.pop("feedback_job", None)
    if fb_jid and fb_jid in JOBS and text:
        job = JOBS[fb_jid]
        if text.startswith("출처:"):
            val = text[3:].strip()
            job["source_override"] = "" if val in ("없음", "삭제", "-") else val
            await produce(ctx, fb_jid, rerender=True)
        elif text.startswith("캡션:"):
            job["caption"] = text[3:].strip()
            await send_caption(ctx, fb_jid)
        else:
            await produce(ctx, fb_jid, feedback=text)
        return

    # 앨범(여러 장 묶음)은 같은 작업으로 모은 뒤 한 번만 물어봄
    albums = ctx.chat_data.setdefault("albums", {})
    gid = msg.media_group_id
    if gid and gid in albums and albums[gid] in JOBS:
        jid = albums[gid]
        is_new = False
    else:
        jid = new_job(chat_id)
        is_new = True
        if gid:
            albums[gid] = jid
    job = JOBS[jid]

    if text:
        prefix = f"[텔레그램 '{name}'에서 전달된 메시지]\n" if name and not job["raw"] else ""
        job["raw"] = (job["raw"] + "\n" + prefix + text).strip()
    if name and name not in job["sources"]:
        job["sources"].append(name)
    job["urls"] += [u for u in extract_urls(msg) if u not in job["urls"]]
    has_media = bool(msg.photo or msg.video or msg.animation or msg.document)
    ack = None
    if has_media and not gid:
        ack = await msg.reply_text("📥 받는 중… (영상이면 조금 걸려요)")
    try:
        m = await download_media(msg, job["dir"] / "src", f"Visual reference: {name}" if name else "")
        if m:
            job["media"].append(m)
    except ValueError as e:
        await msg.reply_text(f"{e}\n영상 없이 글 내용으로만 만들 수 있어요.")
    except Exception as e:
        log.exception("media download failed")
        if has_media:
            await msg.reply_text(f"사진/영상을 못 받았어요 ({e}). 글 내용으로만 만들 수 있어요.")

    if gid:
        if is_new:
            ctx.user_data["last_job"] = jid
            ctx.job_queue.run_once(flush_album, 3, data=jid)
        return
    if not job["raw"] and not job["media"]:
        cleanup(jid)
        await msg.reply_text("텍스트나 사진/영상이 있는 메시지를 보내주세요.")
        return
    ctx.user_data["last_job"] = jid
    if ack:
        await ack.edit_text("어떤 형식으로 만들까요?", reply_markup=kb_format(jid))
    else:
        await msg.reply_text("어떤 형식으로 만들까요?", reply_markup=kb_format(jid))


@authorized
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")
    action, jid = parts[0], parts[-1]
    job = JOBS.get(jid)
    if not job:
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("만료된 작업이에요. 다시 보내주세요.")
        return

    if action == "fmt":
        job["kind"] = parts[1]
        await q.edit_message_text(f"{LABEL[parts[1]]}로 만들게요.")
        await produce(ctx, jid)
    elif action == "ok":
        await q.edit_message_reply_markup(None)
        await publish(ctx, jid)
    elif action == "re":
        await q.edit_message_reply_markup(None)
        await produce(ctx, jid)
    elif action == "sw":
        await q.edit_message_reply_markup(None)
        job["kind"] = "reel" if job["kind"] == "card" else "card"
        job.pop("data", None)
        await produce(ctx, jid)
    elif action == "fb":
        ctx.user_data.pop("source_job", None)
        ctx.user_data["feedback_job"] = jid
        await q.message.reply_text(
            "수정할 내용을 보내주세요. (예: 제목 더 세게, 2번 슬라이드 숫자 빼줘)\n"
            "'캡션:'으로 시작하면 캡션만 그대로 교체합니다.")
    elif action == "src":
        ctx.user_data.pop("feedback_job", None)
        ctx.user_data["source_job"] = jid
        now = ", ".join(job.get("sources") or []) or "없음"
        await q.message.reply_text(
            f"지금 출처: {now}\n\n표시할 출처를 보내주세요. 예: X @idaehye09819729\n"
            "'없음'을 보내면 출처 표시를 뺍니다. 원고는 그대로 두고 이미지/영상만 다시 만들어요.")
    elif action == "file":
        await send_files(ctx, jid)
    elif action == "done":
        await q.edit_message_reply_markup(None)
        cleanup(jid)
        await q.message.reply_text("정리했어요. 다음 소스 보내주세요.")
    elif action == "x":
        await q.edit_message_reply_markup(None)
        cleanup(jid)
        await q.message.reply_text("취소했어요.")


# ---------- 실행 ----------

async def post_init(app: Application):
    if await collector.start():
        tz = ZoneInfo(config.TIMEZONE)
        for t in config.SCHEDULE_TIMES:
            h, m = (int(x) for x in t.split(":"))
            app.job_queue.run_daily(scheduled_collect, time=dtime(h, m, tzinfo=tz))
        log.info(f"자동 수집 예약: {config.SCHEDULE_TIMES} ({config.TIMEZONE})")


async def post_shutdown(app: Application):
    await collector.stop()


def main():
    if not config.ALLOWED_USER_IDS:
        log.warning("ALLOWED_USER_IDS가 비어 있음. 봇에 /start 보내서 내 id 확인 후 .env에 넣으세요.")
    app = (Application.builder().token(config.TELEGRAM_BOT_TOKEN).concurrent_updates(True)
           .read_timeout(60).write_timeout(120).media_write_timeout(600).connect_timeout(30)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler(["card", "reel"], cmd_direct))
    app.add_handler(CommandHandler("collect", cmd_collect))
    app.add_handler(CommandHandler("limit", cmd_limit))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO | filters.ANIMATION
         | filters.Document.VIDEO | filters.Document.IMAGE) & ~filters.COMMAND, on_message))
    log.info("bot started")
    # 파이썬 3.14부터는 이벤트 루프를 직접 만들어줘야 함
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
