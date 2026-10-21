"""처음 한 번만 실행: 내 텔레그램 계정 로그인 (채널 자동 수집용).

python login.py  → 전화번호(+8210...) → 텔레그램 앱으로 온 코드 입력 → (2단계 인증 비번)
"""
from telethon.sync import TelegramClient

import config
from collector import SESSION

with TelegramClient(str(SESSION), config.TG_API_ID, config.TG_API_HASH) as client:
    me = client.get_me()
    print(f"\n로그인 완료: {me.first_name} (@{me.username})\n")
    for ch in config.SOURCE_CHANNELS:
        try:
            e = client.get_entity(ch)
            print(f"  OK   {ch}  →  {getattr(e, 'title', '')}")
        except Exception as ex:
            print(f"  실패 {ch}  →  {ex}")
