# AI Cartoon Pet Video 1.2.3 · 逆向报告（R1）

| 项 | 值 |
|---|---|
| APK | `AI_Cartoon_Pet_Video_1_2_3_modyolo.apk`（modyolo 重打包版） |
| package | `text.to.video.aivideo.generator` |
| versionCode / versionName | 23 / 1.2.3（官方最新已到 25 / 1.2.5） |
| 上游 | `https://t2p.aritek.app/` |
| 备用 IAP 校验 | `https://android-api-verify.aritek.app/` |
| CDN / 产物域名 | `https://r2t2p.aritek.app/`（图片）、`https://sora.aritek.app/`（视频） |
| 技术栈 | Kotlin + Retrofit2 + OkHttp + Gson + Room；广告 SDK：AppLovin / Pangle / AdMob / Unity Ads / Vungle；订阅：Qonversion + Adjust |
| 加固 | 无壳。`libsigner.so`/`libpglarmor.so` 属穿山甲广告 SDK，与业务无关 |
| 分析日期 | 2026-10-29 |

---

## 1. 结论摘要

1. **无账号体系**。鉴权只靠三个 HTTP 头：`Sign` + `Device-Id` + `Ctry-Target`，用 `GET /api/v1/user/info` 换一个 HS256 JWT（`ttl=3600`）。**`user_id` 就等于 `Device-Id`**，任意 16 位 hex 字符串都是一个全新的 guest 用户，带全套免费额度。
2. **`Sign` 是可静态推导的**：等于 APK 签名证书 DER 的小写 hex SHA1，服务端做白名单。mod 包会被 `{"code":-2,"message":"Invalid signature"}` 拒绝；**官方 Play 证书**的 SHA1 `7259cb222a66496242650023583742fe40ea0a80` 直接通过。
3. 因此这个 App 的"付费墙"只建立在两件事上：证书指纹 + 每 device_id 的一次性免费额度。**device_id 可再生 ⇒ 额度可无限续**（R1 已实测）。
4. 文生视频是**同步**接口（3.3–3.8 s 直接返回 mp4 URL），文生图/超分/老化是**异步任务**（`jobId` → `POST /api/v1/generate/status` 轮询）。
5. 服务端存在 **6 个 App 客户端没有入口的隐藏端点**（`image/enhance`、`image/aging`、`image/expand`、`image/object-remover`、`image/open-eyes`、`image/i2i`），其中 `enhance`（4x 超分）与 `aging` 可用且各给 **100 次/设备**免费额度。
6. `i2v` 系列（图生视频 / 换脸 / 跳舞 / lora / r2v）与 `i2i_edit` 是**服务端** 402 Premium 门，伪造 `isPremium=1`、`pt_`、`versionCode` 均无效——门读的是 JWT 的 `is_premium` claim，只有 IAP 校验服务能置位。R1 判定：**不可用，不做进一步绕过**。

---

## 2. 证据链

### 2.1 静态：Retrofit 接口全表

`U9/a.<clinit>` 建了 4 个 OkHttpClient，baseUrl 来自 `SharedPreferences["api_url"]`，默认 `https://t2p.aritek.app/`（即远端可改基址）。接口定义在 `U9/b`：

```text
@GET("api/v1/t2p/home")                     b()  首页 banner / 热门模板（免鉴权）
@GET("api/v1/user/info")                    g()  取 JWT
@GET("api/v1/user/check-limit?type=…")      d()  查额度
@POST("/api/v1/video/t2v")                  f()  文生视频（@Body，Gson JSON）
@POST("/api/v1/video/i2v")                  i()  图生视频（@Multipart）
@POST("/api/v1/image/t2i")                  j()  文生图（JSON {prompt, ctry_target}）
@POST("/api/v1/generate/status")            a()  任务轮询（JSON {ids:[…]}）
@POST("/api/v1/moderation/check-input")     c()  输入审核（multipart）
@POST("/api/v1/moderation/check-output")    e()  输出审核（multipart）
@GET ("api/v1/redeem/check")                k()  兑换码（header Redeem-Code）
@POST("videos/batch")                       h()  客户端调用但服务端 404
```

### 2.2 静态：请求头注入拦截器 `U9/f.intercept`

smali（`dexdump -d classes4.dex`，地址 `325d10`）逐条核对，逐字还原：

```kotlin
val b = original.newBuilder()
    .header("Sign", a.a)                                   // ← Sign 常量
    .header("pt", prefs.getString("pt_", ""))
    .header("Ctry-Target", prefs.getString("ctry_target", "others"))
    .header("versionCode", "23")
    .header("Authorization", "Bearer " + prefs.getString("user_token", ""))
if (prefs.getString("device_id","").isNotEmpty()) b.header("Device-Id", …)
if (prefs.getString("redeem_code","").isNotEmpty()) b.header("Redeem-Code", …)
```

### 2.3 静态：`Sign` 的来源

`da.D.onCreate` (`4c82a8`)：

```kotlin
if (a.a.length == 0) a.a = android.support.v4.media.session.a.s(context)
```

`…session/a.s(Context)`（`43eb94`，逐指令确认）：

```kotlin
val certBytes = pm.getPackageInfo(pkg, GET_SIGNATURES/GET_SIGNING_CERTIFICATES)
                   .signatures[0].toByteArray()          // 证书 DER
    ?: "".toByteArray()
return MessageDigest.getInstance("SHA1").digest(certBytes).toHexString().lowercase()
```

→ **`Sign` = SHA1(APK 证书 DER)，小写 hex**。mod 包重签后值必然改变，所以服务端必然报 `Invalid signature`（实测确认）。

### 2.4 黑盒验证记录（全部实跑）

| 请求 | 结果 |
|---|---|
| `GET /api/v1/t2p/home`（无任何头） | `200 {"code":0,"datas":{"banner":[…]}}` — 完全公开 |
| `GET /api/v1/user/info`（mod 证书 SHA1 `ae846c58…`） | `401 {"code":-2,"message":"Invalid signature"}` |
| `GET /api/v1/user/info`（少头） | `400 "Device-Id, Sign, and Ctry-Target headers are required"` |
| `GET /api/v1/user/info`（官方 SHA1 `7259cb22…`） | `200`，返回 JWT：`{user_id, is_premium:false, device_id, sign, ctry_target, user_type:"guest", subscriptionPlan:"unknown", iat, exp}`，`ttl=3600` |
| `GET /user/check-limit?type=video` | `400` 回显**全部 14 个合法 type** |
| `POST /api/v1/video/t2v` JSON | `200` → `{"url":"https://sora.aritek.app/generated/…mp4"}`，3.84 s |
| `POST /api/v1/image/t2i` JSON | `200` → `{"jobId":"1793285592117_1a38ad3526f94abb"}` |
| `POST /api/v1/generate/status` `{ids:[…]}` | `200` → `status:"succeeded"` + `url` |
| `POST /api/v1/video/i2v` multipart | `402 "Premium subscription required"` |
| `POST /api/v1/image/enhance` multipart | `200` → jobId → 出图 **2048×2048** |
| `POST /api/v1/image/aging` multipart | `200` → jobId → `failed`（上游缺参数） |
| `POST /api/v1/image/expand` / `object-remover` | `502 HTTP error: 409 Conflict`（路由存在、后端不可用） |
| `POST /api/v1/image/open-eyes` | `400 "Valid eye_regions.left coordinates are required"`（参数名泄露） |
| `POST /videos/batch` | `404 Route not found` |
| 换一个新 `Device-Id` | 额度立刻回到 `t2v 10 / t2i 3` |

---

## 3. 协议规格

### 3.1 公共头

| 头 | 必需 | 说明 |
|---|---|---|
| `Sign` | `user/info` 必需 | 官方证书 SHA1（固定） |
| `Device-Id` | `user/info` 必需 | 16 hex，自由生成，**身份主键** |
| `Ctry-Target` | `user/info` 必需 | `others` / `US` / …，不校验取值 |
| `versionCode` | 可选 | 实测不带也能过，带上填 `23` |
| `Authorization` | 业务端点必需 | `Bearer <jwt>`，1 h 过期 |

### 3.2 `POST /api/v1/video/t2v`（JSON，同步）

```json
{
  "prompt": "A chubby orange kitten …",
  "versionCode": 23,
  "deviceID": "<device_id>",
  "isPremium": 0,
  "ctry_target": "others",
  "used": [],
  "aspect_ratio": "9:16",
  "ai_sound": 1
}
```

响应 `{"code":0,"success":true,"data":{"url":"…mp4"}}`。
实测产物：**480×832、16 fps、5.06 s、h264+aac、3.11 MB**（`aspect_ratio` 可选 `9:16` / `16:9` / `1:1`，`ai_sound` 0/1）。

> `isPremium` 是**客户端自报**字段，但只影响该次请求参数，不构成解封（服务端门禁读 JWT）。

### 3.3 `POST /api/v1/image/t2i`（JSON，异步）

请求 `{"prompt": "...", "ctry_target": "others"}` → `{"jobId":"<ts>_<hex>"}`
产物：**1024×1024 PNG，1.18 MB**（固定方图，无尺寸参数）。

### 3.4 `POST /api/v1/generate/status`（JSON，轮询）

请求 `{"ids":["job1","job2"]}`（**必须是数组，`jobId`/`id` 单值会被拒**：`Ids must be a non-empty array`）

```json
{"code":0,"success":true,"data":[
  {"code":0,"jobId":"…","url":"https://r2t2p.aritek.app/generates/20261029/….png","status":"succeeded"},
  {"code":-1,"jobId":"…","url":"","message":"An error occurred while generating your content. Please try again.","status":"failed"}
]}
```

状态机：`processing` → `succeeded` / `failed` / `expired` / `blocked`。t2i 通常 4–6 s 完成。

### 3.5 `POST /api/v1/image/enhance`（multipart）— 隐藏端点

`multipart/form-data` 单字段 `image`（文件）→ jobId → 轮询。
**实测输入 1024×1024 PNG → 输出 2048×2048 JPEG**（4x 超分），耗时 ~13 s，免费额度 **100 次/设备**。

### 3.6 响应错误码

| code | HTTP | 含义 |
|---|---|---|
| 0 | 200 | 成功 |
| -1 | 400 | 参数错误（会回显缺失字段名，非常好的枚举源） |
| -2 | 401 | 鉴权失败（`Invalid signature` / `Missing or invalid Authorization header`） |
| -4 | 404 | 路由不存在（回显 `resource`） |
| -10 | 500 | 服务端内部错误 |
| -13 | 502 | 上游错误（透传 `HTTP error: 409 Conflict`） |
| -21 | 402 | `Premium subscription required` |

---

## 4. 额度矩阵（新 guest，实测）

| check-limit type | 额度 | 对应端点 | 状态 |
|---|---|---|---|
| `t2v_prompt` | **10** | `POST /api/v1/video/t2v` | ✅ 可用 |
| `t2i_prompt` | **3** | `POST /api/v1/image/t2i` | ✅ 可用 |
| `image_enhance` | **100** | `POST /api/v1/image/enhance` | ✅ 可用（隐藏） |
| `image_aging` | **100** | `POST /api/v1/image/aging` | ⚠️ 接受任务，输出 failed |
| `open_eyes` | **10** | `POST /api/v1/image/open-eyes` | ⚠️ 需 `eye_regions.left` 坐标，格式待解 |
| `image_expand` | **5** | `POST /api/v1/image/expand` | ❌ 上游 409 |
| `object_remover` | **5** | `POST /api/v1/image/object-remover` | ❌ 上游 409 |
| `i2i_swap` | **3** | `/api/v1/image/i2i` | ❌ 402 premium |
| `i2v_prompt` | — | `POST /api/v1/video/i2v` | ❌ 402 premium |
| `i2i_edit` / `i2v_swap` / `i2v_dance` / `i2v_lora` / `i2v_r2v` | — | — | ❌ 402 premium |

---

## 5. 为什么这是一个"可池化"的上游

```
Sign        : 固定常量（公开信息，来自官方证书）
Device-Id   : 客户端生成 → 服务端无校验 → 每次新值 = 新用户 = 新免费额度
user_infe   : 无需邮箱/手机/验证码/图形验证
代价        : 0（除 TLS 往返）
```

R1 实测：同一 IP、无任何风控拦截，连续新建 device_id 均立即获得满额。**未观察到 IP 限流、设备指纹、Play Integrity、日配额**（仅做了低频验证，不代表没有软限流）。

---

## 6. 已验证 / 未验证

**已验证（实际运行观察）**
- 三个头的必需性与取值语义
- 官方证书 SHA1 可通过、mod 证书被拒
- 新 device_id ⇒ 新满额额度
- t2v 同步出片（3.3–3.8 s，480×832/16fps/5.06 s/h264+aac）
- t2i 异步出图（1024×1024 PNG）
- enhance 4x 出图（2048×2048 JPEG）
- i2v / i2i 服务端 402
- 网关 mock 压测（20 并发 × 8 s/路由，零失败）：`/health` 858 rps、`/v1/models` 1449 rps、
  `/v1/images/generations` 537 rps、`/v1/videos/generations/sync` 1240 rps
- real 全链路 3 类能力（t2v / t2i / enhance）各 1 次成功，号池轮转与额度记账正确
- demo 页 Playwright E2E（mock + real 双跑通过），截图 `static/demo.png`、`static/demo_real.png`

**未验证（明确列出，不得当结论）**
- `image/aging` 需要什么参数（现在一律 failed）
- `image/open-eyes` 的 `eye_regions` 传输格式
- `image_expand` / `object-remover` 是否永久 409，还是有可用参数组合
- 上游是否存在 IP/设备级软限流（R1 只做了低频采样）
- `videos/batch`（客户端有定义，服务端 404，可能已被移除）
- JWT 的 `is_premium` 是否有其它置位路径（IAP 校验接口未测试，按红线不触碰付费链路）

---

## 7. 合规提示

本次分析对象是第三方生产服务。本仓库的定位是**协议研究 + 本地网关演示**：

- 压测**只打本地网关的 mock 模式**，从不对 `t2p.aritek.app` 施压；
- 真实链路仅做低频功能验证（每项 1 次），不做批量刷额度；
- 产物（视频/图片）版权属上游与用户，本仓库不再分发；
- 若用于生产对接，需自行评估上游服务条款与所在地法律，本项目不承担由此产生的责任。
