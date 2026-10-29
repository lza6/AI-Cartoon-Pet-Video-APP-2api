# AI Cartoon Pet Video · 2API (R1)

把 `text.to.video.aivideo.generator` 1.2.3 的私有协议封装成 OpenAI 风格的本地网关，带 device-id 号池。

## 能力

| 模型 | 说明 | 上游免费额度/设备 |
|---|---|---|
| `t2p/t2v` | 文生视频，同步返回 mp4（480×832 / 16 fps / ~5 s / 带 aac） | 10 |
| `t2p/t2i` | 文生图，1024×1024 PNG（异步任务，网关内部轮询） | 3 |
| `t2p/enhance` | 4x 超分，1024 → 2048 JPEG | 100 |
| `t2p/aging` | 人脸老化（上游输出 failed，暂不可用） | 100 |
| `t2p/i2v` | 图生视频 —— 上游 402 premium，网关返回 501 | 0 |

## 快速开始

```powershell
# 本地合成模式（不碰上游，用来压测/演示）
$env:T2P_MODE="mock"; python server.py

# 真实模式（走 device-id 池）
$env:T2P_MODE="real"; $env:T2P_POOL_SIZE="20"; python server.py
```

打开 <http://127.0.0.1:8950/demo>

```bash
curl -X POST http://127.0.0.1:8950/v1/images/generations \
  -H 'Content-Type: application/json' -d '{"prompt":"cartoon pet cat"}'

curl -X POST http://127.0.0.1:8950/v1/videos/generations/sync \
  -H 'Content-Type: application/json' -d '{"prompt":"kitten dancing","aspect_ratio":"9:16"}'
```

## 文件

```
client.py             上游协议封装（Sign/Device-Id/JWT、t2v/t2i/enhance/aging/status）
server.py             零依赖 HTTP 网关（mock / real / auto），含号池与额度记账
demo.html             可视化 demo（4 个 tab + 号池额度实时看板）
bench.py              本地压测（只打 mock 模式）
verify_demo.py        Playwright 浏览器 E2E
accounts.json         号池（device_id + ctry_target，可手工扩充）
docs/REVERSE-REPORT.md      逆向报告（证据链、端点表、错误码）
docs/NEW-API-INTEGRATION.md new-api 对接指南（两种形态 + 定价 + 排错）
static/demo.png             浏览器 E2E 截图（mock）
static/demo_real.png        浏览器 E2E 截图（真实出图/出片）
bench.mock.json             压测结果
```

## 已验证的数字

- mock 压测（20 并发，8 s/路由，本机，`bench.mock.json`）：
  `/health` 858 rps（p50 14 ms）、`/v1/models` 1449 rps（p50 10.5 ms）、
  `/v1/images/generations` 537 rps（p50 19.7 ms，瓶颈是每次新 prompt 要落一张 PNG）、
  `/v1/videos/generations/sync` 1240 rps（p50 11.6 ms），**全部 200，零失败**。
- real 链路：t2v 3.35 s 出片、t2i 6.16 s 出图、enhance 13.2 s 出 2048² 图；号池轮转与额度记账正确。
- 浏览器 E2E：mock 与 real 两种模式均 PASS。

## 注意

- 压测只能打 mock。`t2p.aritek.app` 是第三方生产服务，按红线只做低频功能验证。
- `Sign` 是**官方证书**的 SHA1（`7259cb222a66496242650023583742fe40ea0a80`）。mod 包重签后算出来的值会被拒。
- 详见 `docs/REVERSE-REPORT.md` 的"已验证 / 未验证"分界。
