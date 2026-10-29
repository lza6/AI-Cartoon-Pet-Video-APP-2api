"""AI Cartoon Pet Video (text.to.video.aivideo.generator) 1.2.3 upstream client.

R1 protocol reconstruction.  Upstream: https://t2p.aritek.app/

Auth model (3 headers, no account system):
    Sign         = lowercase hex SHA1 of the APK signing certificate DER.
                   Server whitelists it -> the ORIGINAL Play certificate works,
                   the modded (modyolo) re-sign does NOT ("Invalid signature").
    Device-Id     = free-form client-generated string.  Any new value mints a
                   brand new guest user with a full free quota.  No registration.
    Ctry-Target   = "others" / "US" / ... free-form, not validated.
    Authorization = "Bearer <jwt>"  <- returned by GET /api/v1/user/info (ttl 3600)

Free quota per Device-Id (verified 2026-10-29):
    t2v_prompt 10 | t2i_prompt 3 | image_enhance 100 | image_aging 100
    open_eyes 10 | image_expand 5 | object_remover 5 | i2i_swap 3
    i2v_prompt / i2i_edit / i2v_swap / i2v_dance / i2v_lora / i2v_r2v -> 402 premium
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

import requests

BASE_URL = "https://t2p.aritek.app"
# SHA1 of the ORIGINAL Play Store signing certificate (CN=Android, O=Google Inc.)
OFFICIAL_SIGN = "7259cb222a66496242650023583742fe40ea0a80"
APP_VERSION_CODE = 23
UA = "okhttp/4.12.0"

# check-limit type names accepted by the server (leaked verbatim by a 400 body)
QUOTA_TYPES = [
    "t2v_prompt",
    "i2v_prompt",
    "t2i_prompt",
    "i2i_edit",
    "image_enhance",
    "image_aging",
    "open_eyes",
    "image_expand",
    "object_remover",
    "i2i_swap",
    "i2v_swap",
    "i2v_dance",
    "i2v_lora",
    "i2v_r2v",
]

TERMINAL_JOB_STATES = {"succeeded", "failed", "expired", "blocked"}


class UpstreamError(RuntimeError):
    """Upstream answered with a business-level failure."""

    def __init__(self, code: int, message: str, http_status: int = 0):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status


class PremiumRequired(UpstreamError):
    pass


@dataclass
class Quota:
    allowed: bool
    remaining: int
    total_limit: int


@dataclass
class Session:
    """One guest identity = one Device-Id."""

    device_id: str
    ctry_target: str = "others"
    token: str = ""
    token_exp: float = 0.0
    is_premium: bool = False
    used: dict[str, int] = field(default_factory=dict)  # type -> consumed count

    @property
    def authorized(self) -> bool:
        return bool(self.token) and time.time() < self.token_exp - 30

    def bump(self, quota_type: str) -> None:
        self.used[quota_type] = self.used.get(quota_type, 0) + 1


def new_device_id() -> str:
    """16 hex chars, matching the shape the app stores in SharedPreferences."""
    return uuid.uuid4().hex[:16]


class T2PClient:
    def __init__(self, session: Session, base_url: str = BASE_URL, timeout: int = 120):
        self.session = session
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self._http = requests.Session()

    # ---------------------------------------------------------------- headers
    def _headers(self, auth: bool = True, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "Sign": OFFICIAL_SIGN,
            "Device-Id": self.session.device_id,
            "Ctry-Target": self.session.ctry_target,
            "versionCode": str(APP_VERSION_CODE),
            "User-Agent": UA,
            "Accept": "application/json",
        }
        if auth and self.session.token:
            h["Authorization"] = "Bearer " + self.session.token
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = self.base_url + path.lstrip("/")
        kwargs.setdefault("timeout", self.timeout)
        resp = self._http.request(method, url, **kwargs)
        try:
            payload = resp.json()
        except ValueError:
            raise UpstreamError(-99, f"non-JSON response ({resp.status_code}): {resp.text[:200]}",
                                resp.status_code) from None
        if not payload.get("success", False):
            code = payload.get("code", -99)
            message = payload.get("message") or payload.get("error") or "unknown error"
            if code == -21:
                raise PremiumRequired(code, message, resp.status_code)
            raise UpstreamError(code, message, resp.status_code)
        return payload.get("data", payload)

    # ------------------------------------------------------------------ auth
    def login(self, force: bool = False) -> Session:
        if self.session.authorized and not force:
            return self.session
        data = self._request("GET", "/api/v1/user/info", headers=self._headers(auth=False))
        self.session.token = data["token"]
        self.session.token_exp = time.time() + int(data.get("ttl", 3600))
        self.session.is_premium = bool(data.get("is_premium", False))
        return self.session

    def check_limit(self, quota_type: str) -> Quota:
        self.login()
        data = self._request(
            "GET", f"/api/v1/user/check-limit?type={quota_type}", headers=self._headers()
        )
        return Quota(
            allowed=bool(data.get("allowed")),
            remaining=int(data.get("remaining", 0)),
            total_limit=int(data.get("total_limit", 0)),
        )

    # ------------------------------------------------------------- home/feed
    def home(self) -> dict:
        """Public, no auth at all."""
        return self._request("GET", "/api/v1/t2p/home", headers=self._headers(auth=False))

    # --------------------------------------------------------------- generate
    def t2v(
        self,
        prompt: str,
        aspect_ratio: str = "9:16",
        ai_sound: int = 1,
        is_premium: int = 0,
        used: list[str] | None = None,
    ) -> str:
        """Text -> video.  Synchronous: the response already carries the mp4 URL."""
        self.login()
        body = {
            "prompt": prompt,
            "versionCode": APP_VERSION_CODE,
            "deviceID": self.session.device_id,
            "isPremium": is_premium,
            "ctry_target": self.session.ctry_target,
            "used": used or [],
            "aspect_ratio": aspect_ratio,
            "ai_sound": ai_sound,
        }
        data = self._request(
            "POST", "/api/v1/video/t2v",
            headers=self._headers(extra={"Content-Type": "application/json"}),
            data=json.dumps(body),
        )
        self.session.bump("t2v_prompt")
        return data["url"]

    def t2i(self, prompt: str) -> str:
        """Text -> image.  Asynchronous: returns a jobId, poll generate/status."""
        self.login()
        body = {"prompt": prompt, "ctry_target": self.session.ctry_target}
        data = self._request(
            "POST", "/api/v1/image/t2i",
            headers=self._headers(extra={"Content-Type": "application/json"}),
            data=json.dumps(body),
        )
        self.session.bump("t2i_prompt")
        return data["jobId"]

    def i2v(
        self,
        image: bytes,
        prompt: str = "",
        aspect_ratio: str = "9:16",
        ai_sound: int = 1,
        filename: str = "image.png",
        mime: str = "image/png",
    ) -> str:
        """Image -> video.  Premium only (402 for guests)."""
        self.login()
        files = {
            "image": (filename, image, mime),
            "prompt": (None, prompt),
            "aspect_ratio": (None, aspect_ratio),
            "ai_sound": (None, str(ai_sound)),
        }
        data = self._request("POST", "/api/v1/video/i2v", headers=self._headers(), files=files)
        self.session.bump("i2v_prompt")
        return data["jobId"]

    def _image_tool(self, endpoint: str, quota_type: str, image: bytes,
                    filename: str = "image.png", mime: str = "image/png",
                    extra: dict[str, str] | None = None) -> str:
        self.login()
        files = {"image": (filename, image, mime)}
        for k, v in (extra or {}).items():
            files[k] = (None, v)
        data = self._request("POST", f"/api/v1/image/{endpoint}", headers=self._headers(), files=files)
        self.session.bump(quota_type)
        return data["jobId"]

    def enhance(self, image: bytes, **kw) -> str:
        """4x super resolution (1024 -> 2048 verified)."""
        return self._image_tool("enhance", "image_enhance", image, **kw)

    def aging(self, image: bytes, **kw) -> str:
        return self._image_tool("aging", "image_aging", image, **kw)

    def expand(self, image: bytes, **kw) -> str:
        """Upstream currently answers 409 Conflict -> marked unusable in R1."""
        return self._image_tool("expand", "image_expand", image, **kw)

    # ------------------------------------------------------------------ jobs
    def status(self, job_ids: list[str]) -> list[dict]:
        self.login()
        data = self._request(
            "POST", "/api/v1/generate/status",
            headers=self._headers(extra={"Content-Type": "application/json"}),
            data=json.dumps({"ids": job_ids}),
        )
        return data if isinstance(data, list) else [data]

    def wait_job(self, job_id: str, timeout: float = 300.0, interval: float = 2.0) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            jobs = self.status([job_id])
            last = jobs[0] if jobs else {}
            if last.get("status") in TERMINAL_JOB_STATES:
                return last
            time.sleep(interval)
        return {**last, "status": last.get("status") or "timeout"}

    # ------------------------------------------------------------ moderation
    def check_input(self, prompt: str = "", image: bytes | None = None, mime: str = "image/png") -> dict:
        self.login()
        files: dict = {}
        if prompt:
            files["prompt"] = (None, prompt)
        if image:
            files["image"] = ("image.png", image, mime)
        return self._request("POST", "/api/v1/moderation/check-input", headers=self._headers(), files=files)

    def check_output(self, image: bytes, mime: str = "image/png") -> dict:
        self.login()
        files = {"image": ("image.png", image, mime)}
        return self._request("POST", "/api/v1/moderation/check-output", headers=self._headers(), files=files)

    def redeem(self, code: str) -> dict:
        return self._request("GET", "/api/v1/redeem/check",
                             headers=self._headers(extra={"Redeem-Code": code}))
