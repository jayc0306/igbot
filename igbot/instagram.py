"""Instagram Graph API 게시: 카드뉴스(캐러셀) / 릴스."""
import time

import httpx

import config


class InstagramError(RuntimeError):
    pass


class Instagram:
    def __init__(self):
        self.uid = config.IG_USER_ID
        self.token = config.IG_ACCESS_TOKEN
        self.base = f"https://{config.IG_GRAPH_HOST}/{config.IG_API_VERSION}"
        self.http = httpx.Client(timeout=60)

    def _req(self, method: str, path: str, **params) -> dict:
        params["access_token"] = self.token
        if method == "GET":
            r = self.http.get(f"{self.base}/{path}", params=params)
        else:
            r = self.http.post(f"{self.base}/{path}", data=params)
        data = r.json()
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            raise InstagramError(f"{err.get('message', r.text)} (code {err.get('code')}, "
                                 f"subcode {err.get('error_subcode')})")
        return data

    def _wait(self, container_id: str, timeout: int = 300):
        """컨테이너가 FINISHED 될 때까지 대기. 영상은 처리에 수십 초~수 분 걸림."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self._req("GET", container_id, fields="status_code,status").get("status_code")
            if st in ("FINISHED", "PUBLISHED"):
                return
            if st in ("ERROR", "EXPIRED"):
                info = self._req("GET", container_id, fields="status")
                raise InstagramError(f"컨테이너 처리 실패: {st} / {info.get('status')}")
            time.sleep(5)
        raise InstagramError("컨테이너 처리 시간 초과")

    def _publish(self, creation_id: str) -> dict:
        media_id = self._req("POST", f"{self.uid}/media_publish", creation_id=creation_id)["id"]
        info = self._req("GET", media_id, fields="permalink")
        return {"id": media_id, "permalink": info.get("permalink")}

    def publish_carousel(self, image_urls: list[str], caption: str) -> dict:
        if not 2 <= len(image_urls) <= 10:
            raise InstagramError("캐러셀은 2~10장이어야 합니다")
        children = [
            self._req("POST", f"{self.uid}/media", image_url=u, is_carousel_item="true")["id"]
            for u in image_urls
        ]
        for c in children:
            self._wait(c, timeout=120)
        cid = self._req("POST", f"{self.uid}/media", media_type="CAROUSEL",
                        children=",".join(children), caption=caption)["id"]
        self._wait(cid, timeout=120)
        return self._publish(cid)

    def publish_reel(self, video_url: str, caption: str, cover_url: str | None = None) -> dict:
        params = dict(media_type="REELS", video_url=video_url, caption=caption, share_to_feed="true")
        if cover_url:
            params["cover_url"] = cover_url
        cid = self._req("POST", f"{self.uid}/media", **params)["id"]
        self._wait(cid, timeout=900)
        return self._publish(cid)

    def publishing_limit(self) -> dict:
        return self._req("GET", f"{self.uid}/content_publishing_limit", fields="config,quota_usage")
