"""Kirim push notification lewat FCM HTTP v1 memakai service account (tanpa SDK Google)."""

import json
import logging
import time
from functools import lru_cache

import httpx
import jwt

from app.core.config import get_settings

logger = logging.getLogger("levelin.push")

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


class FCMSender:
    def __init__(self, service_account_file: str) -> None:
        with open(service_account_file, encoding="utf-8") as fh:
            info = json.load(fh)
        self.project_id = info["project_id"]
        self.client_email = info["client_email"]
        self.private_key = info["private_key"]
        self.token_uri = info.get("token_uri", "https://oauth2.googleapis.com/token")
        self._token: str | None = None
        self._token_exp = 0.0

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        now = int(time.time())
        assertion = jwt.encode(
            {"iss": self.client_email, "scope": SCOPE, "aud": self.token_uri, "iat": now, "exp": now + 3600},
            self.private_key,
            algorithm="RS256",
        )
        resp = await client.post(
            self.token_uri,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_exp = now + int(body.get("expires_in", 3600))
        return self._token

    async def send(self, token: str, title: str, body: str, data: dict | None = None) -> str:
        """Hasil: "sent", "invalid" (token harus dihapus), atau "error"."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                access = await self._access_token(client)
                resp = await client.post(
                    f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send",
                    headers={"Authorization": f"Bearer {access}"},
                    json={
                        "message": {
                            "token": token,
                            "notification": {"title": title, "body": body},
                            "data": {k: str(v) for k, v in (data or {}).items()},
                        }
                    },
                )
            if resp.status_code == 404 or (resp.status_code == 400 and "UNREGISTERED" in resp.text):
                return "invalid"
            return "sent" if resp.status_code < 300 else "error"
        except (httpx.HTTPError, KeyError) as exc:
            logger.warning("FCM send failed: %s", exc)
            return "error"


@lru_cache
def get_fcm() -> FCMSender | None:
    path = get_settings().fcm_service_account_file
    if not path:
        return None
    try:
        return FCMSender(path)
    except (OSError, KeyError, ValueError) as exc:
        logger.warning("FCM disabled: %s", exc)
        return None
