#!/usr/bin/env python3
"""AI Cartoon Pet Video 2API gateway (R1)

Zero-dependency gateway (stdlib only for the server side; `requests` is used only
against the real upstream) that exposes the reconstructed t2p.aritek.app protocol
behind OpenAI-shaped routes, with a device-id pool so the 10 t2v / 3 t2i /
100 enhance free quota per identity is multiplied.

Modes
    mock   no upstream traffic at all - synthetic images/videos, used for load tests
    real   real upstream traffic through the device-id pool
    auto   real, but silently degrade to mock when the pool is exhausted / upstream 5xx

Routes
    GET  /health
    GET  /v1/models
    POST /v1/images/generations            text -> image   (blocks until the job lands)
    POST /v1/images/enhance                image -> 4x     (multipart, form field `image`)
    POST /v1/images/aging                  image -> aged    (multipart)
    POST /v1/images/edits                  alias of /v1/images/enhance
    POST /v1/videos/generations            text -> video   (async job, OpenAI shaped)
    POST /v1/videos/generations/sync       text -> video   (blocking, returns the mp4 url)
    GET  /v1/videos/generations/{id}       poll an async job
    POST /v1/videos/i2v                    premium-only upstream, documented as unavailable
    GET  /native/home                      upstream public feed (pass-through)
    GET  /native/quota                     per-account quota snapshot
    GET  /demo , /static/*                 demo page
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from client import (  # noqa: E402
    QUOTA_TYPES,
    PremiumRequired,
    Session,
    T2PClient,
    UpstreamError,
    new_device_id,
)

MODE = os.environ.get("T2P_MODE", "auto").lower()
PORT = int(os.environ.get("T2P_PORT", "8950"))
POOL_SIZE = int(os.environ.get("T2P_POOL_SIZE", "20"))
ACCOUNTS_FILE = Path(os.environ.get("T2P_ACCOUNTS", HERE / "accounts.json"))
STATIC_DIR = HERE / "static"
MAX_BODY = 32 * 1024 * 1024

# quota that actually works on a fresh guest identity
FREE_QUOTA = {
    "t2v_prompt": 10,
    "t2i_prompt": 3,
    "image_enhance": 100,
    "image_aging": 100,
}


# --------------------------------------------------------------------------- pool
class Account:
    def __init__(self, device_id: str, ctry_target: str = "others"):
        self.session = Session(device_id=device_id, ctry_target=ctry_target)
        self.lock = threading.Lock()
        self.dead = False
        self.error = ""

    @property
    def label(self) -> str:
        return self.session.device_id

    def exhausted(self) -> bool:
        used = self.session.used
        return all(used.get(k, 0) >= v for k, v in FREE_QUOTA.items())

    def client(self) -> T2PClient:
        return T2PClient(self.session)


class AccountPool:
    def __init__(self, size: int):
        self.accounts: list[Account] = []
        self._rr = 0
        self._lock = threading.Lock()
        self._load(size)

    def _load(self, size: int) -> None:
        raw = []
        if ACCOUNTS_FILE.exists():
            try:
                raw = json.loads(ACCOUNTS_FILE.read_text("utf-8"))
            except Exception:
                raw = []
        if not raw:
            raw = [{"device_id": new_device_id(), "ctry_target": "others"} for _ in range(size)]
            ACCOUNTS_FILE.write_text(json.dumps(raw, indent=2), "utf-8")
        self.accounts = [Account(a["device_id"], a.get("ctry_target", "others")) for a in raw]

    def acquire(self, quota_type: str) -> Account | None:
        """Round-robin over accounts that still have quota left for `quota_type`."""
        with self._lock:
            n = len(self.accounts)
            for i in range(n):
                idx = (self._rr + i) % n
                acc = self.accounts[idx]
                if acc.dead:
                    continue
                if acc.session.used.get(quota_type, 0) >= FREE_QUOTA.get(quota_type, 10**9):
                    continue
                self._rr = (idx + 1) % n
                return acc
        return None

    def snapshot(self) -> list[dict]:
        out = []
        for acc in self.accounts:
            out.append({
                "device_id": acc.label,
                "ctry_target": acc.session.ctry_target,
                "is_premium": acc.session.is_premium,
                "used": dict(acc.session.used),
                "dead": acc.dead,
                "error": acc.error,
            })
        return out


POOL = AccountPool(POOL_SIZE)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def mock_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def mock_image_png(prompt: str, size: int = 1024) -> bytes:
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (size, size), (24 + size % 40, 28, 46))
        d = ImageDraw.Draw(img)
        for i in range(0, size, 64):
            d.line([(0, i), (size, i)], fill=(70, 90, 140), width=1)
            d.line([(i, 0), (i, size)], fill=(70, 90, 140), width=1)
        d.rectangle([size // 8, size // 3, size * 7 // 8, size * 2 // 3], outline=(200, 220, 255), width=4)
        d.text((size // 8 + 16, size // 3 + 16), "MOCK " + prompt[:40], fill=(230, 240, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        # 1x1 transparent png fallback
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
            "60e6kgAAAABJRU5ErkJggg=="
        )


def mock_video_path() -> Path:
    p = STATIC_DIR / "mock.mp4"
    if p.exists() and p.stat().st_size > 1000:
        return p
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import shutil
        import subprocess
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            subprocess.run(
                [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=0x1b1f2e:s=480x832:d=3:r=16",
                 "-vf", "drawtext=text='MOCK T2V':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2",
                 "-pix_fmt", "yuv420p", "-c:v", "libx264", str(p)],
                capture_output=True, timeout=60, check=False,
            )
    except Exception:
        pass
    return p


# ------------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "T2P2API/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        if os.environ.get("T2P_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, ctype: str = "application/json",
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _error(self, status: int, message: str, code: str = "error") -> None:
        self._json(status, {"error": {"message": message, "type": code, "code": status}})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(length) if length else b""

    def _body_json(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _multipart_image(body: bytes, content_type: str) -> tuple[bytes, str]:
        """Tiny multipart parser: pulls the first file part out of the body."""
        boundary = None
        for part in content_type.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "boundary":
                boundary = v.strip('"')
        if not boundary:
            raise ValueError("multipart boundary missing")
        marker = b"--" + boundary.encode()
        for chunk in body.split(marker):
            if b"\r\n\r\n" not in chunk:
                continue
            head, _, data = chunk.partition(b"\r\n\r\n")
            if b"filename=" not in head:
                continue
            mime = "image/png"
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-type:"):
                    mime = line.split(b":", 1)[1].strip().decode()
            return data.rstrip(b"\r\n"), mime
        raise ValueError("no file part found")

    # -- routing ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/health":
                return self._json(200, {"status": "ok", "mode": MODE,
                                        "accounts": len(POOL.accounts),
                                        "jobs": len(JOBS)})
            if path == "/v1/models":
                return self._json(200, {"object": "list", "data": MODELS})
            if path == "/native/quota":
                return self._json(200, {"object": "list", "accounts": POOL.snapshot()})
            if path == "/native/home":
                return self._json(200, self._native(lambda c: c.home()))
            if path.startswith("/v1/videos/generations/"):
                job_id = path.rsplit("/", 1)[-1]
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                if not job:
                    return self._error(404, "job not found", "not_found")
                return self._json(200, job)
            if path in ("/demo", "/demo.html", "/"):
                return self._static("demo.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):], None)
            if path.startswith("/v1/jobs/"):
                return self._error(404, "use /v1/videos/generations/{id}", "not_found")
            return self._error(404, f"no route {path}", "not_found")
        except Exception as exc:  # pragma: no cover - defensive
            traceback.print_exc()
            return self._error(500, str(exc), "internal_error")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/v1/images/generations":
                return self._images_generations()
            if path in ("/v1/images/enhance", "/v1/images/edits"):
                return self._image_tool("enhance", "image_enhance")
            if path == "/v1/images/aging":
                return self._image_tool("aging", "image_aging")
            if path == "/v1/images/expand":
                return self._image_tool("expand", "image_expand")
            if path == "/v1/videos/generations":
                return self._videos_async()
            if path == "/v1/videos/generations/sync":
                return self._videos_sync()
            if path == "/v1/videos/i2v":
                return self._i2v()
            return self._error(404, f"no route {path}", "not_found")
        except PremiumRequired as exc:
            return self._error(402, f"upstream premium gate: {exc.message}", "premium_required")
        except UpstreamError as exc:
            return self._error(502, f"upstream {exc}", "upstream_error")
        except ValueError as exc:
            return self._error(400, str(exc), "invalid_request")
        except Exception as exc:  # pragma: no cover - defensive
            traceback.print_exc()
            return self._error(500, str(exc), "internal_error")

    # -- static -------------------------------------------------------------
    def _static(self, name: str, ctype: str | None) -> None:
        target = (STATIC_DIR / name) if name != "demo.html" else (HERE / "demo.html")
        if not target.exists() or not target.resolve().is_relative_to(HERE.resolve()):
            return self._error(404, "not found", "not_found")
        if ctype is None:
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".mp4": "video/mp4",
                ".png": "image/png",
                ".jpg": "image/jpeg",
            }.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    # -- upstream helpers ---------------------------------------------------
    def _native(self, fn) -> dict:
        if MODE == "mock":
            return {"code": 0, "mock": True}
        acc = POOL.acquire("t2v_prompt") or (POOL.accounts[0] if POOL.accounts else None)
        if acc is None:
            raise RuntimeError("account pool empty")
        return fn(acc.client())

    def _run_real(self, quota_type: str, fn):
        """Pick an account, run `fn(client)`, rotate on quota errors."""
        last_exc: Exception | None = None
        for _ in range(max(3, len(POOL.accounts))):
            acc = POOL.acquire(quota_type)
            if acc is None:
                raise UpstreamError(-1, "all pooled identities exhausted their free quota", 429)
            with acc.lock:
                try:
                    return fn(acc.client())
                except PremiumRequired:
                    raise
                except UpstreamError as exc:
                    last_exc = exc
                    if exc.code in (-2, -21):
                        acc.session.token = ""  # force re-login next time
                    elif "quota" in (exc.message or "").lower() or "limit" in (exc.message or "").lower():
                        acc.session.used[quota_type] = FREE_QUOTA.get(quota_type, 10**9)
                    else:
                        acc.dead = True
                        acc.error = str(exc)
        raise last_exc or UpstreamError(-1, "no usable account")

    # -- route implementations ---------------------------------------------
    def _images_generations(self) -> None:
        body = self._body_json()
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        n = max(1, min(int(body.get("n", 1)), 4))
        if MODE == "mock":
            urls = [f"{self._base()}/static/{self._mock_file(prompt)}" for _ in range(n)]
        else:
            def run(c: T2PClient):
                job = c.t2i(prompt)
                return c.wait_job(job)

            urls = []
            for _ in range(n):
                res = self._real_or_mock("t2i_prompt", run, prompt)
                if res.get("status") != "succeeded":
                    return self._error(502, res.get("message") or "t2i failed", "upstream_error")
                urls.append(res["url"])
        if (body.get("response_format") or "url") == "b64_json":
            data = [{"b64_json": self._fetch_b64(u)} for u in urls]
        else:
            data = [{"url": u} for u in urls]
        self._json(200, {"created": int(time.time()), "data": data})

    def _image_tool(self, endpoint: str, quota_type: str) -> None:
        ctype = self.headers.get("Content-Type", "")
        image: bytes | None = None
        if ctype.startswith("multipart/form-data"):
            image, _mime = self._multipart_image(self._body(), ctype)
        else:
            body = self._body_json()
            if body.get("image_b64"):
                image = base64.b64decode(body["image_b64"].split(",", 1)[-1])
        if not image:
            raise ValueError("image is required (multipart field `image` or JSON image_b64)")
        if MODE == "mock":
            return self._json(200, {"created": int(time.time()),
                                    "data": [{"url": f"{self._base()}/static/{self._mock_file(endpoint)}"}]})

        def run(c: T2PClient):
            job = getattr(c, endpoint)(image)
            return c.wait_job(job)

        res = self._real_or_mock(quota_type, run, endpoint)
        if res.get("status") != "succeeded":
            msg = res.get("message") or f"{endpoint} failed"
            if "409" in msg:
                msg = (f"{endpoint}: upstream answers 409 Conflict for every payload - "
                       f"the endpoint exists but has no working backend (R1 finding)")
            return self._error(502, msg, "upstream_error")
        self._json(200, {"created": int(time.time()), "data": [{"url": res["url"]}]})

    def _videos_sync(self) -> None:
        body = self._body_json()
        prompt = (body.get("prompt") or "").strip() or (body.get("input") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        aspect = body.get("aspect_ratio") or body.get("size") or "9:16"
        sound = int(body.get("ai_sound", 1))
        if MODE == "mock":
            return self._json(200, {"created": int(time.time()),
                                    "data": [{"url": f"{self._base()}/static/mock.mp4"}]})

        def run(c: T2PClient):
            return {"url": c.t2v(prompt, aspect_ratio=aspect, ai_sound=sound)}

        res = self._real_or_mock("t2v_prompt", run, prompt)
        self._json(200, {"created": int(time.time()), "data": [{"url": res["url"]}]})

    def _videos_async(self) -> None:
        body = self._body_json()
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        job_id = mock_id("vid")
        with JOBS_LOCK:
            JOBS[job_id] = {"id": job_id, "object": "video.generation", "status": "processing",
                            "created_at": int(time.time()), "prompt": prompt}
        threading.Thread(target=self._run_video_job, args=(job_id, body), daemon=True).start()
        self._json(200, JOBS[job_id])

    def _run_video_job(self, job_id: str, body: dict) -> None:
        try:
            if MODE == "mock":
                mock_video_path()
                url = f"{self._base()}/static/mock.mp4"
            else:
                aspect = body.get("aspect_ratio") or "9:16"
                sound = int(body.get("ai_sound", 1))
                prompt = body["prompt"]

                def run(c: T2PClient):
                    return {"url": c.t2v(prompt, aspect_ratio=aspect, ai_sound=sound)}

                url = self._real_or_mock("t2v_prompt", run, prompt)["url"]
            with JOBS_LOCK:
                JOBS[job_id].update(status="completed", url=url, completed_at=int(time.time()))
        except Exception as exc:
            with JOBS_LOCK:
                JOBS[job_id].update(status="failed", error=str(exc), completed_at=int(time.time()))

    def _i2v(self) -> None:
        body = self._body_json()
        return self._error(
            501,
            "i2v / i2v_swap / i2v_dance / i2v_lora / i2v_r2v / i2i_edit are gated server-side "
            "with HTTP 402 'Premium subscription required' (verified: forging body isPremium=1 or "
            "the versionCode/pt_ headers does not lift it - the gate reads the JWT is_premium claim "
            "that only the IAP verification service can set)",
            "premium_required",
        )

    def _real_or_mock(self, quota_type: str, fn, prompt: str) -> dict:
        """real first; in auto mode fall back to a synthetic result instead of failing."""
        if MODE == "mock":
            return self._mock_result(quota_type, prompt)
        try:
            return self._run_real(quota_type, fn)
        except Exception:
            if MODE == "auto":
                return self._mock_result(quota_type, prompt)
            raise

    def _mock_result(self, quota_type: str, prompt: str) -> dict:
        if quota_type.startswith("t2v"):
            mock_video_path()
            return {"status": "succeeded", "url": f"{self._base()}/static/mock.mp4"}
        return {"status": "succeeded", "url": f"{self._base()}/static/{self._mock_file(prompt)}"}

    def _mock_file(self, seed: str) -> str:
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        name = "mock_%s.png" % (uuid.uuid5(uuid.NAMESPACE_OID, seed).hex[:12])
        target = STATIC_DIR / name
        if not target.exists():
            target.write_bytes(mock_image_png(seed))
        return name

    def _fetch_b64(self, url: str) -> str:
        import requests
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return base64.b64encode(r.content).decode()

    def _base(self) -> str:
        host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
        return f"http://{host}"


MODELS = [
    {"id": "t2p/t2v", "object": "model", "owned_by": "aritek", "type": "video",
     "description": "text->video, sync mp4, 480x832 16fps ~5s (free 10/identity)"},
    {"id": "t2p/t2i", "object": "model", "owned_by": "aritek", "type": "image",
     "description": "text->image 1024x1024 (free 3/identity)"},
    {"id": "t2p/enhance", "object": "model", "owned_by": "aritek", "type": "image",
     "description": "4x upscale 1024->2048 (free 100/identity)"},
    {"id": "t2p/aging", "object": "model", "owned_by": "aritek", "type": "image",
     "description": "face aging (free 100/identity)"},
    {"id": "t2p/i2v", "object": "model", "owned_by": "aritek", "type": "video",
     "description": "image->video - upstream 402 premium, not usable"},
]


def main() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    if MODE != "mock":
        mock_video_path()

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 512  # default 5 overflows under burst concurrency

    srv = Server(("0.0.0.0", PORT), Handler)
    print(f"[t2p-2api] mode={MODE} port={PORT} accounts={len(POOL.accounts)} "
          f"file={ACCOUNTS_FILE}")
    print(f"[t2p-2api] demo   http://127.0.0.1:{PORT}/demo")
    print(f"[t2p-2api] models http://127.0.0.1:{PORT}/v1/models")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[t2p-2api] bye")


if __name__ == "__main__":
    main()
