# USAGE.md · 开发者手册

面向在本仓库写代码的人：怎么装、怎么跑、契约长什么样、配置怎么给、鉴权怎么过、
测试怎么跑，以及**改策略之前必须先做什么**。

运维部署看 [DEPLOYMENT.md](DEPLOYMENT.md)；产品规则看 [GUIDE.md](GUIDE.md)；协作约定
看 [AGENTS.md](../AGENTS.md)；架构总览看 [README.md](../README.md)。

---

## 1. 环境与安装

要求 **Python ≥ 3.12**。包内依赖只有 `httpx`、`PyYAML`、`aiohttp`、`cryptography`、
`qrcode[pil]`；**不依赖** `raricy_bot`、OpenAI、MCP 或旧仓库的任何文件。

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e .[dev]
```

- `pip install -e .` 装运行时；`[dev]` 额外带 `pytest` / `pytest-asyncio`。
- 不装包也能跑工具脚本（它会把仓库 `src/` 临时放进导入路径），但**测试与入口都以
  安装后的包为准**。
- 依赖齐全后可用控制台脚本 `raricy-capital`，与 `python -m raricy_capital` 等价。

## 2. 目录结构

```
raricy_capital/
├── README.md  AGENTS.md  CLAUDE.md               项目与协作约定
├── pyproject.toml                                 打包与 pytest 配置
├── src/raricy_capital/                            唯一源码包
│   ├── __main__.py                                独立入口
│   ├── data_lock.py                               OS 生命周期单写者锁
│   ├── contracts.py  config.py  store.py          精度/策略、配置、存储
│   ├── ledger.py                                  记账核心
│   ├── client.py  adapters.py                     站点客户端与交易侧桥接
│   ├── commands.py  payments.py                   命令处理、收付与通知
│   ├── trader.py  strategy.py                     交易运行时与纯策略规则
│   ├── operations.py  runtime.py  web.py          运维面、装配、运营台
│   ├── static/                                    运营台前端资源
│   ├── chat_models.py                         站内消息 DTO
│   ├── site_protocol.py                       练手盘页面解析 + 站点时间换算
│   ├── hourly_signals.py                      小时信号与结算公式
│   └── safe_logging.py                        安全堆栈
├── tools/run_capital_service.py                   运维 CLI（薄封装）
├── tests/                                         针对性测试（直接 import raricy_capital）
├── packaging/funds/                               部署物料（systemd / 计划任务 / 样例配置）
├── docs/                                          USAGE、DEPLOYMENT、GUIDE、INTRODUCTION
│   ├── research/                                 历史证据与口径
│   └── assets/                                   研究图表
└── data/capital_funds/                            默认数据目录（本机状态，不进版本库）
```

约定：**资金模块直接放在 `src/raricy_capital/` 下**；入口 `__main__.py` 直接导入
`data_lock`，其它资金模块按需从同级的 chat_models、site_protocol、hourly_signals 和 safe_logging 导入。

## 3. 运行入口

**主入口**（唯一正式入口；信号处理、数据锁与装配都在这里）：

```bash
python -m raricy_capital --config packaging/funds/config.example.yaml
```

| 开关 | 作用 |
| --- | --- |
| `--config <path>` | YAML/JSON 配置；字段见 §6 |
| `--data-dir <path>` | 覆盖数据目录（默认 `data/capital_funds`） |
| `--port <n>` | 覆盖控制台端口（默认 `8137`） |
| `--live` | 打开外部写总闸（真实私聊、转账、模拟交易）；**默认关闭** |
| `--backup` | **停机状态**下做一次备份后退出（同样先取 OS 锁） |

启动后打印控制台地址与运行模式（`live` / 只读预览），并提示管理令牌的位置。
根入口**没有** `--check`；配置预检由工具脚本提供。

**运维 CLI**：

```bash
python tools/run_capital_service.py --config <config.yaml> check         # 预检：不打开数据库、不取锁
python tools/run_capital_service.py --data-dir data/capital_funds status # 状态：不打开数据库
python tools/run_capital_service.py --data-dir data/capital_funds list-backups
python tools/run_capital_service.py --data-dir data/capital_funds backup [--label x] [--keep N]
python tools/run_capital_service.py --data-dir data/capital_funds export [--out name.zip]
python tools/run_capital_service.py --data-dir data/capital_funds verify <bundle.zip>
python tools/run_capital_service.py --data-dir data/capital_funds restore <bundle.zip> [--target DIR] [--force]
python tools/run_capital_service.py --config <config.yaml> serve <根入口的开关原样转发>
```

- `--config` 优先于 `--data-dir`；都没有时用公开默认值。
- `backup` / `export` / `restore` 会打开 SQLite，因此**先取同一把 OS 锁**：服务在线时
  以 `service_running` 拒绝（退出码 `3`），绝不并发写。
- `status` / `list-backups` / `verify` / `check` 只读、不打开数据库、不取锁。
- `restore` 探测的是**目标**目录上的锁，并要求目标为空或显式 `--force`。

## 4. 契约

### 4.1 精度与错误

`contracts.py` 是全局口径的唯一出处：

| 名称 | 值 | 说明 |
| --- | --- | --- |
| `MONEY_SCALE` | `10000` | 金额最小单位 `1e-4`；金额是**整数** `*_units` |
| `SHARE_SCALE` | `100000000` | 份额最小单位 `1e-8`；份额是**整数** `*_shares_atoms` |
| `BEIJING` | UTC+8 | 业务日窗口、月末与紧急批次的时区 |
| `now_ms()` | — | 真实 UTC 毫秒 |
| `money_units(value, positive=False)` | → `int` | 只接受可精确表示到 4 位小数的数值；拒绝 `bool`、非有限值、超 `1e12`、超 4 位小数；`positive=True` 时拒绝 `<= 0`；失败抛 `FundError('invalid_amount')` |
| `money_text(units)` | → `str` | 转成 4 位小数字符串，用于展示与站点请求体 |
| `timestamp_text(ms)` | → `str` | 北京时 ISO 文本，用于提示与日志 |

**错误统一走 `FundError(code)`**：`code` 是稳定类别码（如 `invalid_amount`、
`insufficient_shares`、`deadline_passed`、`writer_active`、`policies_are_frozen`），
调用方按码分支；异常里不带上游正文、金额或凭据。

### 4.2 冻结的基金策略

`contracts.py::POLICIES` 是 v0.3 的冻结值，配置里改不动：

| 字段 | `capital1` | `capital2` |
| --- | --- | --- |
| `label` | 温和增长 · ER12 | 激进增长 · 5倍 |
| `leverage` | 3 | 5 |
| `risk` | 0.0125 | 0.05 |
| `cap` | 2.0 | 5.0 |
| `daily_loss` | 0.015 | 0.05 |
| `drawdown_limit` | 0.25 | 0.70 |
| `loss_gate_hours`（ER12 亏损门） | 12 | 0（无此门） |
| `cooldown_hours` | 4 | 4 |
| `max_hold_days` | 14 | 14 |
| `ema_hours` / `slope_hours` / `atr_period` / `atr_stop` | 96 / 24 / 14 / 2.0 | 同左 |
| `subscription_fee` | 0.05 | 0.05 |
| `emergency_fee` | 0.10 | 0.10 |
| `dividend_fraction` | 0.10 | 0.10 |
| `monthly_redemption_fraction` | 0.20 | 0.20 |

策略规则本身是纯函数：`strategy.py`（仓位 `entry_quantity_units`、止损 `stop_price`、
冷却 `cooldown_ready`、ER12 门 `loss_gate_allows`、信号新鲜度 `signal_is_fresh`、
流量归一 `nav_units_after_flow`）与 `包内支持模块/hourly_signals.py`（`closed_signal` /
`payout_units` / `HOUR` / `DAY` / `FEE`）。两者都不做 I/O、不改调用方状态。

### 4.3 账务不变量（`ledger.py`）

- **净值口径**：`equity = wallet + position − pending_receipts − fee_balance −
  unclaimed − liabilities`。已宣告未支付的分配记为负债，付款时只清负债，不二次扣净值。
- **资本流**：`capital_flows_units` 只在真实外部流量上变动（种子出资、已发行申购本金、
  赎回与现金分红）；**已到账未发行不算流量**，发行时才计入。紧急赎回留存费是非投资
  收益，单独记在 `non_trading_income_units`。
- **净值时效**：估值引用不得早于 `max_quote_age_ms = 15s`，也不得晚于当前时间；
  月末与紧急批次的价格必须落在各自的截止窗口内。
- **截止快照**：月末（以及紧急批次的 20:00）在第一次写入时冻结持有人与账目快照；
  截止后的行情永远不能冒充截止价，无效估值只会让该期挂起等待可重试的真实截止前报价。
- **月度赎回上限**：按截止时旧份额的 20% 比例确认，超出部分按比例缩减并顺延下月。
- **分红门槛**：只有当月自身投资利润为正、且净值高于基准（`benchmark_nav`）时才可分配，
  可分配额取「已实现利润」与「高于基准的部分」的较小值，再按 `dividend_fraction` 计提。
- **紧急赎回**：18:00 截止申请、20:00 估值；现金不足时**整批顺延**，不注销、不计费，
  未执行部分可在截止后撤回；显式传 `allow_partial=True`（持有人同意）才允许按比例执行；
  清仓式赎回（一批取走全部在外份额）免收紧急费。
- **幂等键**：私聊消息 ID → 订单；`transfer_id` → 收款；`trade_id`/`position_id` → 交易；
  同一键重放返回原记录，不会重复动账。

## 5. 存储

`store.py` 是一份 SQLite（WAL、`synchronous=FULL`、`foreign_keys=ON`）上的**单写者**：

- `records(namespace, key, value, updated_ms)`：所有业务状态（`funds`、`holders`、
  `subscriptions`、`redemptions`、`payouts`、`notices`、`fund_trader`、`ops`…）。
- `audit_events`：追加式审计事件，带稳定 `event_key`（幂等）与脱敏详情。
- `transaction()`：`BEGIN IMMEDIATE`，嵌套时自动退化为 `SAVEPOINT`；业务代码必须把
  一次业务的所有写入放进同一个事务。
- `backup(path)`：走 SQLite **online backup API**；绝不复制运行中的 WAL 库。

**跨进程单写者**由 `data_lock.py` 提供：入口在**打开 SQLite 之前**调用
`acquire_data_lock(data_dir)`，对 `<data_dir>/.raricy-data.lock` 取 OS 排他锁
（Windows `msvcrt.locking` / POSIX `flock`）。锁随进程退出（含崩溃）由内核释放，
**不存在需要人工清理的陈旧锁**，也不存在「force 夺锁」这条路。

## 6. 配置

配置是 YAML 或 JSON，由 `config.py::FundConfig` 加载校验；样例见
`packaging/funds/config.example.yaml`。**未知字段、非回环 `host`、越界取值都会让启动
直接失败**，不会静默取默认值。

| 配置键 | 默认 | 取值与含义 |
| --- | --- | --- |
| `data_dir` | `data/capital_funds` | 数据目录；会被创建（POSIX 下 `0700`） |
| `host` | `127.0.0.1` | **必须恰为 `127.0.0.1`**，否则 `loopback_required` |
| `port` | `8137` | `1024..65535` |
| `site_url` | `https://raricy.com` | 必须是 https，且不带账号、查询串或片段 |
| `live` | `false` | 外部写总闸 |
| `tick_seconds` | `4` | 主循环与健康探测间隔，`1..60` |
| `backup_interval_seconds` | `3600` | 服务内部备份周期，`>= 60` |
| `backup_retention` | `72` | 备份保留份数，`1..10000` |
| `outage_failure_threshold` | `3` | 连续失败几次判定断线 |
| `log_max_bytes` | `10485760` | 单段日志字节上限，超出即轮转 |
| `log_backup_count` | `10` | 滚动日志保留段数 |
| `control_token` | 空 | 见 §7；来自 `FUNDS_CONTROL_TOKEN` 或数据目录 `admin.token`，长度 `>= 24` |

**兼容别名**（旧键名会被规范化，不必改配置也能读）：

| 旧键 | 归一为 |
| --- | --- |
| `backup_keep` | `backup_retention` |
| `log_keep` | `log_backup_count` |
| `health_interval_seconds` | `tick_seconds` |
| `health_failure_threshold` | `outage_failure_threshold` |

两个**只读校验键**：`fund_ids` 出现时必须与 `POLICIES` 完全一致；`policies` 出现时
必须与冻结值逐字段相等，否则报 `policies_are_frozen`——这是防止有人把策略偷偷改掉的
显式闸门。运维模块读的是 `backup_keep` / `log_keep` / `health_interval_seconds` /
`health_failure_threshold` 这几个**属性别名**，与上面的表一一对应。

### 环境变量

| 变量 | 用途 |
| --- | --- |
| `FUNDS_CONTROL_TOKEN` | 运营台控制令牌；与数据目录 `admin.token` 二选一 |
| `FUNDS_CREDENTIAL_KEY` | 覆盖凭据库的 Fernet 密钥（base64），容器化部署用 |
| `FUNDS_CAPITAL1_USERNAME` / `FUNDS_CAPITAL1_PASSWORD` | capital1 的站点账号 |
| `FUNDS_CAPITAL2_USERNAME` / `FUNDS_CAPITAL2_PASSWORD` | capital2 的站点账号 |

用户名以 `changeme` 开头会被忽略（占位符不会被当成真实账号）。两只基金必须是**两个
不同的站点账号**：登录时会校验账号稳定 ID 不同，发现串号直接拒绝。

### 凭据与令牌

- **凭据库**：`<data_dir>/credentials.enc`（Fernet 加密的账号口令）与
  `<data_dir>/credential.key`（密钥，`FUNDS_CREDENTIAL_KEY` 可覆盖）。写入是
  「临时文件 + `fsync` + `rename`」，POSIX 下权限 `0600`。**两者必须成对保管**，
  都**不在**迁移包里。
- **管理令牌**：首次启动若既无 `FUNDS_CONTROL_TOKEN` 也无 `admin.token`，会生成
  一个并写入 `<data_dir>/admin.token`（`0600`）。它是**独立凭据**，不随迁移包或凭据库
  迁移。
- 密码只用于登录请求；会话 Cookie 只存在内存里，不落盘、不进日志。

## 7. 运营台与鉴权

运营台是一个 aiohttp 应用（`web.py::create_app(service)`），**自己不绑端口**，监听由
运行时负责；默认只有 `http://127.0.0.1:8137/` 可达。远程访问请走 SSH 隧道。

三层守卫，缺一不可：

1. **Host 白名单**：`Host` 头必须是 `localhost` / `127.0.0.1` / `::1`，否则
   `403 forbidden_host`（防 DNS rebinding）。
2. **写请求同源**：`POST`/`PUT`/`PATCH`/`DELETE` 必须带同源 `Origin` 头
   （否则 `403 forbidden_origin`）且 `Content-Type: application/json`
   （否则 `415 unsupported_media_type`），防 CSRF。
3. **控制令牌**：除 `/api/login` 与 `/api/logout` 外，所有 `/api/*` 都要鉴权 ——
   `Authorization: Bearer <token>`，或登录换来的会话 Cookie。

登录流程：

```bash
# 用控制令牌换一个短期会话 Cookie（TTL 8 小时，HttpOnly + SameSite=Strict）
curl -sS -X POST http://127.0.0.1:8137/api/login \
  -H "Origin: http://127.0.0.1:8137" -H "Content-Type: application/json" \
  -d "{\"token\": \"$FUNDS_CONTROL_TOKEN\"}" -c cookie.jar

# 之后带 Cookie 读取状态
curl -sS -b cookie.jar http://127.0.0.1:8137/api/status
```

路由一览：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/`、`/static/*` | 运营台页面与静态资源 |
| `POST` | `/api/login`、`/api/logout` | 会话登录 / 登出（豁免鉴权） |
| `GET` | `/api/status` | 基金、事件、网络、运行状态的脱敏快照 |
| `GET` | `/api/orders` | 订单列表（可按 `fund_id` / `user_id` 过滤） |
| `GET` | `/api/events` | 审计事件（`limit`、`fund_id`） |
| `GET` | `/api/holders` | 某基金持有人 |
| `GET` | `/api/funds/{fund_id}/nav-history` | 净值曲线数据 |
| `POST` | `/api/funds/{fund_id}/login` | 登录该基金的站点账号 |
| `POST` | `/api/funds/{fund_id}/seed` | 机构首次出资（金额字符串，服务端转整数单位） |
| `POST` | `/api/funds/{fund_id}/running` | 启停新开仓（`{"enabled": true/false}`） |
| `POST` | `/api/funds/{fund_id}/stop` | 停机（排队平掉自有仓位） |
| `POST` | `/api/funds/{fund_id}/settle` | 手动结算（`kind: month`/`emergency`） |
| `POST` | `/api/dividend-choice` | 设置某持有人的分红复投比例 |
| `POST` | `/api/backup` | 触发一次运行中的备份 |

所有响应都会递归剔除疑似密钥的键；统一附加 `Content-Security-Policy`、
`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy:
no-referrer`、`Cache-Control: no-store`。

## 8. 测试

针对性优先，够用即止：

```bash
# 账务不变量：分红、截止快照、申购/退款、20% 赎回门、紧急费与豁免、复投、重放
python -m pytest tests/test_ledger.py -q

# 运维面：备份轮转、迁移包安全边界、单写者锁、健康探测、脱敏日志
python -m pytest tests/test_operations.py -q

# 单个用例
python -m pytest tests/test_ledger.py::test_example_month_dividends_match_plan -q
```

`pyproject.toml` 里的 pytest 配置：`pythonpath = ["src"]`（不装包也能 import）、
`asyncio_mode = "auto"`（异步用例不用手写 `@pytest.mark.asyncio`）、
`filterwarnings = ["error"]`（**警告即失败**）。第三点最容易踩：新增代码不要引入未处理
的警告（例如给 aiohttp 的 `app[...]` 用字符串键会直接报 `NotAppKeyWarning`）。

测试直接 `import raricy_capital`，放在 `tests/` 下。**本手册不粘贴测试输出**；结果以你
本机实际运行输出为准。跑不了的（环境/权限限制）就如实报告，不要伪造。

## 9. 扩展策略：先做研究，再动代码

策略改动直接对应真实资金，流程是**研究 → 冻结 → 实现 → 文档**，不是「顺手调个参数」：

1. **先研究**。新的入场/出场条件、仓位公式、风控阈值都必须先有独立的研究与回测
   （数据范围、成交假设、手续费、滑点、样本外与压力情形），结论写进
   [INTRODUCTION.md](INTRODUCTION.md) 与 `docs/research/`。
2. **再冻结**。研究通过后才把参数落到 `contracts.py::FundPolicy` / `POLICIES`，并把
   冻结口径写进文档；历史研究的结果**不能**当作新规则的证据，新旧口径必须分开表述。
3. **然后实现**。纯规则改 `strategy.py` / `包内支持模块/hourly_signals.py`；涉及外部副作用的
   改 `trader.py`，并保持「意图先落盘、未知结果先对账、同键重试」这三条不变。
4. **配套测试**。为新规则补针对性用例；**警告即失败**，不要靠放宽断言过关。

另外三条底线：

- **拆分/迁移/重构期间不改策略与财务规则**。这类改动会被 `policies` 校验键和评审直接
  挡下。
- **不要动两套规则之外的自由度**：`POLICIES` 只有 capital1 / capital2，加基金属于产品
  变更，需要明确授权。
- **不要为了跑通测试而改规则**。规则是研究对象，测试是它的守门人，不能反过来。

## 10. 排障速查

| 现象 | 含义与处理 |
| --- | --- |
| `服务未启动：loopback_required` | `host` 不是 `127.0.0.1`；控制台不允许绑其它地址 |
| `服务未启动：weak_control_token` | 令牌短于 24 位（占位符常见）；换真实令牌或删掉 `admin.token` 让它重生成 |
| `操作被拒绝：writer_active` | 目标数据目录正被某个进程持锁；先停服务 |
| 工具脚本退出码 `3` + `service_running` | 服务在线，离线写操作被正确拒绝；改用控制台 `POST /api/backup` |
| `invalid_configuration` | 配置里有未知字段，或字段类型/取值越界 |
| `policies_are_frozen` | 配置里写了 `policies` 且与冻结值不一致；删掉这个键 |
| `fund_accounts_must_differ` | 两只基金配了同一个站点账号；换独立账号 |
| `account_identity_changed` | 同一基金换了账号 ID；需要人工确认后再继续 |
| `nav_unavailable` / `pending_valuation` | 没有 15 秒内的有效估值；结算是挂起而非用错价 |
| `qrcode_unavailable` | 未装 `qrcode[pil]`；支付链接仍会作为文本发出 |
| `unsupported`（HTTP 501） | 运营台调用了 service 未提供的能力，属装配缺失而非配置问题 |
