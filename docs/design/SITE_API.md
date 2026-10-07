# FundSiteClient 站点接口契约

实现：`src/raricy_capital/client.py`
测试：`tests/test_client.py`（httpx.MockTransport，零真实网络）

上游权威：[raricycms/raricy.com](https://github.com/raricycms/raricy.com)，研究时读取的源码提交为 `7ffdf55acd69c15e9d393eb125d9407f92ec6b7c`。
本文件描述的是**本客户端实际发送与解析的形状**；与源码不符时以源码为准，并回来修本文件。

## 1. 定位与复用

`FundSiteClient` 持有一条内存会话，同时打通三族接口：

| 族 | 路由前缀 | 源码入口 |
|----|----------|----------|
| 鱼干市场（转账 / 余额 / 流水） | `/api/fish/market/*` | `src/app/api/fish/market/**/route.ts`、`src/lib/fish-market-service.ts`、`src/lib/fish-service.ts` |
| 讨论（私聊 / 消息 / 图床） | `/api/chat/*`、`/api/images` | `src/app/api/chat/**/route.ts`、`src/app/api/images/route.ts` |
| 练手盘（行情 / K 线 / 买卖） | `/api/fish/trade/*`、`/fish/trade` | `src/app/api/fish/trade/**/route.ts`、`src/lib/market-service.ts` |
| 收银台（支付链接） | `/fish/pay` | `src/app/fish/pay/page.tsx`、`src/lib/fish-market-service.ts` |

复用而不复制：

* 聊天 DTO 直接用 `raricy_capital.chat_models.ChatMessage`（`fetch_messages` 的返回类型）。
* 练手盘 HTML 快照解析用 `raricy_capital.site_protocol.parse_trade_page`；站点时间换算用
  `raricy_capital.site_protocol.site_time_ms`。
* 未复用 `SiteClient` / `TradeClient` 的会话与请求层：它们各自持有 httpx、各管一个用途，
  而本子域需要**同一条**会话 + 可注入 `base_url`/`MockTransport`，且必须把金额规范成整数单位。
  两个既有客户端的行为一个字节都没改。

## 2. 构造、生命周期与会话

```python
FundSiteClient(base_url, username, password, *, transport=None, timeout=20.0)
await client.start()          # 创建 httpx.AsyncClient；幂等
user = await client.login()   # {'id', 'username'}
await client.close()          # 幂等；也支持 async with
```

* `base_url` 只接受 http/https + host，且不得含 userinfo / query / fragment。
* `transport` 注入 `httpx.MockTransport` 供测试；生产不传。
* **会话是内存态**：Cookie 只保存在 `client.session_cookie`，不落盘、不进日志；密码只在
  `POST /api/auth/login` 的请求体里出现，业务请求一律不带密码。
* 除 `login()` 外，所有方法都会按需自动登录一次（`_ensure_login`）。收到 401 时**重登一次
  再重放同一请求**（401 是服务端执行前的拒绝，重放安全），其余错误一律不自动重试。
* 不自设 `Origin` / `Referer`，不自设 `Accept-Encoding`（CSRF 对两者都缺失的原生客户端放行）。

登录身份复核：`login()` 在 `POST /api/auth/login` 之后调用 `GET /api/auth/me`，两次返回的
`user.id` 必须一致，否则 `identity_mismatch`。这是双基金各自账号的串号防线。

## 3. 金额与时间口径

* **金额一律整数 1e-4 单位**（`funds/contracts.py` 的 `MONEY_SCALE`，与站点 `fish-units.ts` 的
  `FISH_UNIT_SCALE=10000` 一致）。返回值里所有金额字段后缀 `_units`，是 int。
  发往站点的金额用 `money_text()` 转成 4 位小数字符串（站点 `AMOUNT_RE` 上限 4 位小数）。
  绝不做浮点累加。
* **时间有两把尺子，不可混用**：
  * 流水 `created_at`、练手盘 `opened_at`（源码 `entryQuoteAt`）都由站点 `nowForDb()`
    写入（`db-time.ts`）= 「UTC+8 墙上时间贴 Z 标签」的**假 UTC** → `transactions().occurred_ms`、
    `buy().opened_ms`、`snapshot().positions[].opened_ms` 都减 8 小时，返回**真实 UTC 毫秒**。
  * 只有 K 线 `openTime` 是交易所给的真 UTC → `candles()` 保持原值，绝不减 8 小时。

## 4. 方法一览

### 4.1 资金

| 方法 | 请求 | 返回 |
|------|------|------|
| `await balance() -> int` | `POST /api/fish/market/balance`，body `{}` | 余额整数单位 |
| `await transactions(since_id) -> dict` | `POST /api/fish/market/transactions`，body `{"since_id": int, "limit": 100}` | 见下 |
| `await transfer(user_id, amount_units, note, key) -> dict` | `POST /api/fish/market/transfer` | 见下 |

`transactions` 返回 `{"transactions": [...], "next_cursor": int, "has_more": bool}`；每行：

```python
{"id": int|None, "transfer_id": str|None, "from_user_id": str|None,
 "amount_units": int,          # 正入账 / 负支出
 "note": str|None,             # 从 description 的「：」后取出；非转账行为 None
 "occurred_ms": int|None,      # 真实 UTC 毫秒；无时间戳的行是 None
 "type": str}                  # transfer / transfer_receive / checkin / ...
```

`from_user_id` 的权威来源：`transfer_receive` 行取站点填的 `related_user_id`（对方=发送者）；
`transfer` 行取本账户自身 ID。**绝不从备注或用户名认人**（备注是任何人都能写的自由文本）。

`transfer` 返回 `{"transfer_id": str, "amount_units": int, "duplicated": bool}`。

### 4.2 讨论与图片

| 方法 | 请求 | 返回 |
|------|------|------|
| `await private_channels() -> list[str]` | `GET /api/chat/poll` | `kind == "direct"` 的频道 id（不含 `lobby`） |
| `await fetch_messages(channel_id, after=None) -> list[ChatMessage]` | `GET /api/chat/channels/{id}/messages?limit=100[&after=]` | 站内 DTO，按 id 升序 |
| `await send_message(channel_id, content, reply_to=None, image_bytes=None) -> dict` | 有图先 `POST /api/images`，再 `POST /api/chat/channels/{id}/messages` | 站点返回的消息对象 |
| `await upload_image(image_bytes, *, filename=None, mime=None) -> dict` | `POST /api/images`（multipart，字段 `file`，带 `compress=0`） | `{"id", "url"}`（url 为相对路径） |
| `await upload_qr(image_bytes, *, filename=None, mime=None) -> dict` | 同上 | 同上 |

`image_bytes` 默认按字节内容判断 PNG / SVG（也可显式传 `mime`）；站点图床白名单为
`image/png|jpeg|gif|webp|svg+xml`，单文件上限 10 MiB。`compress=0` 走原图入库，避免二维码
被重压缩糊边。`send_message` 在站点返回 200 但缺少消息对象时抛 `send_unconfirmed`
（`reconcile=True`）—— 消息**可能已经发出**。

### 4.3 收银台与二维码

```python
await client.pay_url(user_id, amount_units, note, order_key) -> str
FundSiteClient.qr_png(text, *, box_size=8, border=2) -> bytes   # 本地渲染 PNG
```

`pay_url` 拼 `{base_url}/fish/pay?to=<基金账号用户名>&amount=<4位小数>&order=<order_key>[&note=]`。

* 收款人恒为本客户端登录的**基金账号**；`user_id`（付款会员）**不写进 URL**。会员身份只能由
  到账流水的 `from_user_id` 认定，URL 与备注都不可信。`user_id` 只做两项校验：非空、且不等于
  基金自身 ID（站点对「给自己付款」同样是明确拒绝）。
* `order_key` 是收银台防重的唯一手段：1–32 位 `[A-Za-z0-9_.:-]`，一笔业务一个订单号。同订单号
  + 同金额刷新重付不会重复扣款；同订单号换金额明确报错。
* 二维码：站点只有 `GET /api/poster/collect` 这一处服务端二维码，且**无参数**（只能生成登录账号
  自己的静态收款码），渲染不了带金额/订单号的收银台链接。因此订单级二维码由 `qr_png` 本地渲染，
  再经 `upload_qr` 上传，最后用 `send_message(..., image_bytes=...)` 发给用户。

### 4.4 练手盘

| 方法 | 请求 | 返回 |
|------|------|------|
| `await quote() -> (float price, float fee_rate)` | `GET /api/fish/trade/quote` | 展示价与费率；`ok=false`/`stale`/年龄>10s 抛 `quote_unavailable` |
| `await candles(symbol=SYMBOL, interval=DEFAULT_INTERVAL) -> list[tuple]` | `GET /api/fish/trade/candles` | `[(open_ms, o, h, l, c, v), ...]`，路由固定上限 1000 根（`CANDLE_LIMIT`）；**末根是当前未走完的桶，策略须丢弃** |
| `await snapshot() -> dict` | `GET /fish/trade`（HTML） | 见下 |
| `await buy(amount, leverage, key) -> dict` | `POST /api/fish/trade/buy` | 见下 |
| `await sell(position_id) -> dict` | `POST /api/fish/trade/sell` | 见下 |

常量：`SYMBOL="BTCUSDT"`、`DEFAULT_INTERVAL="1h"`、`MARKET_INTERVALS=("1m","5m","15m","1h","4h","1d")`、
`LEVERAGES=(1,2,3,5,10,20,100)`（站点白名单，非白名单值本地拒绝，不就近取整）。

`quote` 返回的价**只用于风控判断**，真实成交价由服务端在下单那一刻现取，并出现在 `buy`/`sell`
的 `entry_price` / `exit_price` 里。站点这份展示缓存的阈值是 `QUOTE_STALE_MS=40s`（REST 轮询
15s、WS 帧信任窗 `STREAM_TRUST_MS=10s`，见 `market-price.ts`）；本客户端再收紧一档，只接受
`age_ms <= 10_000`，更旧一律抛可重试的 `quote_unavailable`。

`snapshot` 返回：

```python
{
  "balance_units": int,
  "fee_rate": float,
  "min_stake_units": int,
  "leverage_options": list[int],
  "leverage_enabled": bool,
  "positions": [
    {"position_id", "symbol", "stake_units", "entry_price",
     "liquidation_price", "leverage", "opened_ms"},   # opened_ms 真 UTC
  ],
}
```

购物盘**没有**「列出我的持仓」JSON 接口（`docs/bot/trade-bot.md` §8.2），所以快照只能解析
`/fish/trade` 页面渲染的 props；页面结构变化时抛 `snapshot_unavailable`，绝不猜。

`buy` 返回 `{"position_id","symbol","stake_units","entry_price","leverage","liquidation_price",
"opened_ms","balance_units","replayed"}`。开仓流水里 `related_user_id` 为空、无对手方。

`sell` 返回 `{"position_id","symbol","payout_units","profit_units","exit_price","liquidated",
"replayed","balance_units"}`。`liquidated=true` 且 `payout_units=0` 表示仓位**早已被强平**，不是
本次卖出成交 —— 两者不能并进同一档。

## 5. 幂等、重试与对账

| 操作 | 服务端幂等 | 未知结果（超时 / 连接失败 / 5xx）时 |
|------|-----------|--------------------------------------|
| `transfer` | 带 `key` 时按「发送者 + key」去重；同键同参数回报原单（`duplicated=true`），同键换参数 409 | `retryable=True`：**用同一个 key 原样重发是安全的**；`reconcile=True`：也可先查流水确认 |
| `buy` | 带 `key` 时按 open_key 去重，重放回报原仓位 | `retryable=True` + `reconcile=True`：同键重发安全，仍建议对账 |
| `sell` | **天然幂等**（无键）：仓位结清后再平是重放，不动钱 | `reconcile=True`：**必须先对账**（查流水与快照）确认是否已结清，再决定是否用同一 `position_id` 重发 |
| 余额 / 流水 / 快照 / 报价 | 只读 | 直接重试 |

**绝不自带重试**：本客户端只在 401 时重登并重放一次；其余错误如实抛出，由业务层决定重试或
对账。转账幂等键必须跟着「这笔业务」走（例如提现/申购单号），**不要**用时间戳这种每次都不同的值。

## 6. 失败类别（`FundSiteError.code`）

`str(exc)` 与 `repr(exc)` 就是类别码，**绝不包含上游 `message`、金额、备注或凭据**。

| code | status | 含义 | retryable | reconcile |
|------|--------|------|:---------:|:---------:|
| `network` / `timeout` | 0 | 传输层失败，结果不确定 | ✅ | ✅ |
| `invalid_request` | 400 | 参数被拒（含金额精度、余额不足） | ❌ | ❌ |
| `unauthorized` | 401 | 重登后仍 401 | ❌ | ❌ |
| `forbidden` | 403 | 禁言 / 非 core+ / 专注模式 | ❌ | ❌ |
| `not_found` | 404 | 收款人 / 持仓不存在 | ❌ | ❌ |
| `conflict` | 409 | 幂等键已用于另一笔（换键重来） | ❌ | ❌ |
| `rate_limited` | 429 | 限频，`retry_after` 给秒数 | ✅（退避） | ❌ |
| `server_error` | 5xx | 服务端故障，结果不确定 | ✅ | ✅ |
| `quote_unavailable` | 200 | 行情不可用 / 陈旧 / 太旧 | ✅ | ❌ |
| 其余本地码 | — | `amount_invalid` / `note_too_long` / `key_invalid` / `order_key_invalid` / `self_transfer` / `self_payment` / `invalid_recipient` / `invalid_payer` / `invalid_channel` / `invalid_position` / `invalid_cursor` / `interval_invalid` / `leverage_invalid` / `empty_message` / `invalid_image` / `image_too_large` / `identity_mismatch` / `login_failed` / `missing_session_cookie` / `transfer_unconfirmed` / `buy_unconfirmed` / `sell_unconfirmed` / `send_unconfirmed` / `snapshot_unavailable` / `snapshot_too_large` / `invalid_response` / `qrcode_unavailable` / `qr_render_failed` | — | — |

## 7. 站点限频（源码 `rate-limit.ts`，本客户端不绕过）

* 转账：每发送者 100 次/小时、500 次/天（服务账号白名单 500/小时、5000/天）。
* 下单（买+卖共桶）：20 次/分钟、300 次/24 小时；重放与参数校验失败不消耗额度。
* 发消息：120 次/分钟、8000 次/24 小时；图床 200 张/小时。
* 会话轮询 `/api/chat/poll`：120 次/分钟（全站最重接口，正常几十秒一次）。
* 密码登录路径每账号 20 次/分钟（每次请求跑一次 scrypt，成功也计数）—— 本客户端业务请求
  **不带密码**，只走会话，因此不受这条 CPU 闸门限制。

## 8. 诚实的边界（源码事实，不做假装）

* **没有「列出我的持仓」JSON 接口**，快照靠解析 `/fish/trade` 页面 props，页面改版即失效。
* **没有服务端订单级二维码**；`/api/poster/collect` 只出登录账号的静态收款码。
* **收银台没有机器人可撤销机制**：链接一旦开出，只能靠超时后按流水对账退款。二维码链接的
  180 秒有效期是业务层（ledger/commands）的约束，站点不强制。
* **`pay_url` 不编码付款会员**：站点收银台只认 `to/amount/order/note`；会员归属由到账流水的
  `from_user_id` 认定。
* **没有按 `transfer_id` 查单的接口**；对账走游标流水（`transactions`）。
* **K 线路由固定 1000 根上限（`CANDLE_LIMIT`），不做懒加载**；且末根是当前**未走完**的桶
  （站点只保证末根落在当前桶里），EMA/ATR 只能取已收盘的小时，必须丢弃末根。行情是给页面用的
  缓存展示，策略应另接交易所公开行情。
* **`sell` 没有幂等键**，但服务端天然幂等；未知结果仍须对账。
* **禁止/降权/专注模式由站方决定**：被禁言时转账/开仓 403、平仓与行情仍可达（站点刻意不对称）。

## 9. 未决契约问题（如实记录）

1. 冻结签名 `pay_url(user_id, amount_units, note, order_key)` 里的 `user_id` 语义，计划草案未写死。
   本实现按「付款会员的稳定 ID」解释：不写进 URL，只做非空与「非基金自身」校验。若上层实际传入
   的是收款基金账号 ID（另一种读法），会命中 `self_payment` —— 集成时需与本模块口径对齐。
2. `snapshot()` 的键名计划里只约定 `->dict`，本实现自定了一套 `*_units` + 价格 float 的规范化形状
   （见 §4.4）。trader（D）与运行时装配需按此文件为准，若要改键名须同步改本文件与测试。
3. `qr_png` 依赖 `qrcode[pil]`（已在 `pyproject.toml` 的 `funds` extra 里声明）。未安装时
   `qr_png` 抛 `qrcode_unavailable`；本模块导入与其他方法不受影响。


## 10. 2026-10-07 上游多空与杠杆扩展

核对提交：`d2331679f4886bc96ed79aec4928938fae56437e`。上游现在支持开仓
`direction=long|short`（省略默认long），杠杆为1–100整数；快捷按钮列表不再是合法值白名单。
手续费仍按平仓名义仓位收取一次，空头强平价为开仓价×(1＋1/杠杆)，1倍空头也会强平。

本客户端与基金交易链仍采用冻结的旧多头协议、杠杆白名单，本节说明上游差异。
本次没有改动客户端、真实账号或运行参数。上线多空前需同时适配持久交易意图、仓位方向、
双向估值/止损/强平及未知结果对账，不能只改开仓请求。

源码：[方向与杠杆](https://github.com/raricycms/raricy.com/blob/d2331679f4886bc96ed79aec4928938fae56437e/src/lib/market-leverage.ts)、
[结算公式](https://github.com/raricycms/raricy.com/blob/d2331679f4886bc96ed79aec4928938fae56437e/src/lib/market-math.ts)。
研究结果见[多空优化报告](../materials/research/long_short_2026-10-07/REPORT.md)。
