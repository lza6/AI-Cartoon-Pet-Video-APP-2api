#!/usr/bin/env python3
"""Local load test for the t2p 2API gateway.

Only ever points at the LOCAL gateway, and the gateway must run with T2P_MODE=mock:
we never drive load against the real t2p.aritek.app (that is a third-party service
with a per-identity free quota).

    python server.py                     # T2P_MODE=mock T2P_PORT=8950
    python bench.py --url http://127.0.0.1:8950 --concurrency 20 --duration 15
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request

RESULTS: list[tuple[str, float, int]] = []
LOCK = threading.Lock()


def one(url: str, route: str, payload: bytes | None, timeout: float) -> None:
    t0 = time.perf_counter()
    status = 0
    try:
        req = urllib.request.Request(
            url + route,
            data=payload,
            headers={"Content-Type": "application/json"} if payload else {},
            method="POST" if payload else "GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception:
        status = 0
    elapsed = (time.perf_counter() - t0) * 1000
    with LOCK:
        RESULTS.append((route, elapsed, status))


def worker(url: str, route: str, payload: bytes | None, deadline: float, timeout: float) -> None:
    while time.time() < deadline:
        one(url, route, payload, timeout)


def summarize(route: str) -> dict:
    rows = [r for r in RESULTS if r[0] == route]
    if not rows:
        return {}
    lat = sorted(r[1] for r in rows)
    ok = sum(1 for r in rows if 200 <= r[2] < 300)
    p = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))]  # noqa: E731
    return {
        "route": route,
        "requests": len(rows),
        "ok": ok,
        "fail": len(rows) - ok,
        "rps": round(len(rows) / DURATION, 1),
        "p50_ms": round(p(0.50), 2),
        "p95_ms": round(p(0.95), 2),
        "p99_ms": round(p(0.99), 2),
        "max_ms": round(lat[-1], 2),
        "mean_ms": round(statistics.fmean(lat), 2),
    }


def main() -> None:
    global DURATION
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8950")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--duration", type=float, default=15.0)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    DURATION = args.duration

    routes = [
        ("GET", "/health", None),
        ("GET", "/v1/models", None),
        ("POST", "/v1/images/generations", json.dumps({"prompt": "bench cat"}).encode()),
        ("POST", "/v1/videos/generations/sync", json.dumps({"prompt": "bench cat"}).encode()),
    ]

    report = []
    for _method, route, payload in routes:
        RESULTS.clear()
        deadline = time.time() + args.duration
        threads = [
            threading.Thread(target=worker, args=(args.url, route, payload, deadline, args.timeout),
                             daemon=True)
            for _ in range(args.concurrency)
        ]
        started = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        row = summarize(route)
        row["wall_s"] = round(time.time() - started, 2)
        report.append(row)
        print(json.dumps(row, ensure_ascii=False))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print("saved ->", args.out)


if __name__ == "__main__":
    main()
