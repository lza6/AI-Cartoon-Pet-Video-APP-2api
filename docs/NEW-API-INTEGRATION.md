# new-api 对接指南 · AI Cartoon Pet Video（t2p）

上游：`https://t2p.aritek.app/`
网关：`D:/apk-analysis/ai-cartoon-pet/2api/server.py`（默认端口 8950，可 `T2P_PORT` 覆盖）

本文件是**交接说明书**，不含已落地的 new-api 代码改动。R1 只交付上游协议 + 独立网关 + demo + 压测。

---

## 0. 先选接入形态

| 方案 | 做法 | 优点 | 缺点 | R1 状态 |
|---|---|---|---|---|
| **A. OpenAI 兼容渠道**（推荐先跑通） | new-api 建 `type=1 (OpenAI)` 渠道，`base_url` 指向本网关 | 零代码改动，`/v1/images/generations` 原生可用 | 视频需走异步路由，取决于 new-api 对 `/v1/videos` 的支持 | 网关侧已验证 |
| **B. 任务插件渠道** | 新增 `ChannelType=74` + `plugins/tasks/t2p/plugin.js` | 与 aivg(69)/imagenai(67) 一致，视频任务结算可控 | 需改 Go 常量 + 前端渠道选项 | 未实施，本文件给规格 |

74 号目前空闲（最大已用 73 = `ChannelTypeDuck`）。若与其它分支的 SnapClip 冲突，顺延 75。

---

## 1. 启动网关

```powershell
cd D:\apk-analysis\ai-cartoon-pet\2api

# 真实模式（走 device-id 池）
$env:T2P_MODE="real"; $env:T2P_PORT="8950"; $env:T2P_POOL_SIZE="20"
Start-Process python -ArgumentList "server.py" -WorkingDirectory (Get-Location) -WindowStyle Hidden `
  -RedirectStandardOutput server.log -RedirectStandardError server.err

# 自检
curl.exe http://127.0.0.1:8950/health
curl.exe http://127.0.0.1:8950/v1/models
curl.exe http://127.0.0.1:8950/native/quota    # 看每个 device_id 的额度消耗
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `T2P_MODE` | `auto` | `mock` / `real` / `auto`（auto = 真实优先，池空或上游 5xx 时降级为合成产物） |
| `T2P_PORT` | `8950` | 监听端口 |
| `T2P_POOL_SIZE` | `20` | 首次生成 `accounts.json` 时的 device_id 数量 |
| `T2P_ACCOUNTS` | `./accounts.json` | 号池文件路径 |
| `T2P_VERBOSE` | 空 | 非空则打印访问日志 |

> **端口提示**：本机 89xx 段已被多个历史 demo 占用（8901/8911/8916/8920/8940/8942/8945/8955/8960/8990），启动前先 `netstat -ano | findstr :8950` 确认。R1 实测用 18000/18001。

---

## 2. 网关路由

| 路由 | 语义 | 上游 |
|---|---|---|
| `GET /v1/models` | 能力清单 | — |
| `POST /v1/videos/generations/sync` | `{prompt, aspect_ratio, ai_sound}` → `{data:[{url}]}` | `/api/v1/video/t2v`（同步出片） |
| `POST /v1/videos/generations` | 同上但异步：返回 `{id,status:"processing"}` | 同上（线程内跑） |
| `GET /v1/videos/generations/{id}` | 轮询 `processing` / `completed` / `failed` | — |
| `POST /v1/images/generations` | `{prompt, n, response_format}` → `{data:[{url\|b64_json}]}`，内部阻塞轮询 | `/api/v1/image/t2i` + `/generate/status` |
| `POST /v1/images/enhance`（别名 `/v1/images/edits`） | multipart `image` → 4x 超分 `{data:[{url}]}` | `/api/v1/image/enhance` |
| `POST /v1/images/aging` | multipart `image` | `/api/v1/image/aging`（上游输出 failed，暂不可用） |
| `POST /v1/images/expand` | multipart `image` | 上游 409，网关返回 502 + 原因 |
| `POST /v1/videos/i2v` | 恒返回 501 + 原因 | 上游 402 premium |
| `GET /native/quota` | 号池额度快照 | — |
| `GET /native/home` | 上游首页 feed 透传 | `/api/v1/t2p/home` |
| `GET /demo` | 可视化 demo | — |

---

## 3. 方案 A：OpenAI 兼容渠道（字段级）

渠道设置页：

| 字段 | 值 |
|---|---|
| 类型 | `OpenAI` |
| 名称 | `AI Cartoon Pet Video (t2p)` |
| 代理/Base URL | `http://127.0.0.1:8950` |
| 密钥 | 任意非空串（如 `t2p-local`），网关不校验 |
| 模型 | `t2p/t2v`、`t2p/t2i`、`t2p/enhance`（与 `/v1/models` 一致） |
| 模型重定向 | 如需要把对外模型名映射到上述三个 |
| 分组/倍率 | 见第 5 节定价 |

调用示例：

```bash
# 文生图
curl http://127.0.0.1:8950/v1/images/generations -H 'Content-Type: application/json' \
  -d '{"prompt":"A cute cartoon pet cat","model":"t2p/t2i"}'

# 文生视频（同步）
curl http://127.0.0.1:8950/v1/videos/generations/sync -H 'Content-Type: application/json' \
  -d '{"prompt":"A chubby kitten dancing","aspect_ratio":"9:16","ai_sound":1}'

# 4x 超分
curl -F image=@input.png http://127.0.0.1:8950/v1/images/enhance
```

> 若 new-api 的视频任务路径不认识 `/v1/videos/generations/sync`，先用 `/v1/images/generations`（t2i）+ `/v1/images/edits`（enhance）把图片链路跑通，视频走方案 B。

---

## 4. 方案 B：任务插件渠道（规格）

1. `constant/channel.go` 追加 `ChannelTypeT2P = 74`；
2. 前端 `web/src/constants/channel.constants.ts` 追加渠道项（key=74）；
3. `plugins/tasks/t2p/plugin.js` 声明：

```js
module.exports = {
  name: 't2p',
  channelTypes: [74],
  models: ['t2p/t2v', 't2p/t2i', 't2p/enhance'],
  // 提交任务：POST {base_url}/v1/videos/generations  → {id}
  // 查询任务：GET  {base_url}/v1/videos/generations/{id}
  // 计费单位：按 task 成功结算
};
```

网关已经按 OpenAI 任务形状返回（`id` / `status` / `url`），插件里只需做字段搬运，不需要理解 t2p 原生协议。

---

## 5. 定价建议（防爆）

上游真实成本 ≈ 0（免费额度池化），但**额度是真的有限**：一个 device_id 只有 `10 次 t2v / 3 次 t2i / 100 次 enhance`。

| 能力 | 建议计费 | 理由 |
|---|---|---|
| `t2p/t2v`（5 s 480×832 带音） | 每次 ≥ 1 万 token 等价 | 池化 20 号 = 200 次，通缩快 |
| `t2p/t2i`（1024² PNG） | 每次 ≈ 4000 token 等价 | 单号仅 3 次，最紧 |
| `t2p/enhance`（4x） | 每次 ≈ 800 token 等价 | 单号 100 次，最富余 |

运营动作：
- `GET /native/quota` 定时巡检，剩余低于 20% 时自动把新 device_id 追加进 `accounts.json` 并重启网关（或做热加载）；
- 上游出现 `-21` 时不要重试，直接在渠道层熔断，避免无效扣费；
- `T2P_MODE=auto` 只在**调试**时用：线上开 auto 会把上游失败静默变成合成图，用户会拿到假产物。

---

## 6. 验收四步

```powershell
# 1. 网关活着
curl.exe -s http://127.0.0.1:8950/health
# → {"status":"ok","mode":"real","accounts":20,...}

# 2. 能力清单
curl.exe -s http://127.0.0.1:8950/v1/models

# 3. 真实出图（消耗 1 次 t2i 额度）
curl.exe -s -X POST http://127.0.0.1:8950/v1/images/generations `
  -H "Content-Type: application/json" -d '{\"prompt\":\"cartoon pet cat\"}'
# → {"data":[{"url":"https://r2t2p.aritek.app/generates/...png"}]}

# 4. 号池记账是否正确
curl.exe -s http://127.0.0.1:8950/native/quota
# → 某个 device_id 的 used.t2i_prompt 应为 1
```

浏览器侧：`http://127.0.0.1:8950/demo` → 切 tab → 点发送 → 出图/出片。
自动化：`python verify_demo.py --url http://127.0.0.1:8950`（Playwright）。

---

## 7. 排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| `{"code":-2,"message":"Invalid signature"}` | 用了 mod 包证书 SHA1 | `client.py` 里 `OFFICIAL_SIGN` 必须是官方值 `7259cb22…` |
| `Missing or invalid Authorization header` | 没先调 `user/info` 或 token 过期（1 h） | 网关已自动续期；自写脚本先 `login()` |
| `Ids must be a non-empty array` | status 请求体写成 `{"jobId":…}` | 必须 `{"ids":["…"]}` |
| `402 Premium subscription required` | i2v / i2i / dance / lora / r2v 是服务端付费门 | 不要尝试伪造，网关直接 501/402 返回 |
| `502 … 409 Conflict` | `image/expand`、`image/object-remover` 上游无可用后端 | 在渠道里禁用这两个模型 |
| 端口起不来 / health 返回别人的 JSON | 89xx 段被历史 demo 占用 | 换端口（18000+）或先 `Stop-Process` |
| 压测数字很低 | 用了 real 模式打真实上游 | 压测必须 `T2P_MODE=mock` |

---

## 8. 交接清单

- [x] 上游协议全表 + 三个鉴权头的语义 + 官方证书 SHA1
- [x] `client.py`（协议封装）/ `server.py`（网关）/ `demo.html`（演示）/ `bench.py`（压测）/ `verify_demo.py`（E2E）
- [x] `docs/REVERSE-REPORT.md`（含证据链与已验证/未验证分界）
- [x] 本地 mock 压测数字（`bench.mock.json`）
- [x] real 链路三类能力各一次成功（t2v / t2i / enhance）
- [ ] new-api 侧代码改动（方案 A 或 B）——**未实施，等指示**
- [ ] `image/aging` 参数、`open-eyes` 坐标格式——留 R2
