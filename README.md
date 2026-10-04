# Raricy Capital · 双基金资金服务

本仓库是 Raricy 站内两只小鱼干基金 **capital1（温和增长 · ER12）** 与
**capital2（激进增长 · 5 倍）** 的独立资金服务。它从原 `raricy_bot` 工程里把与这两只
基金相关的代码整体拆分出来，单独打包、单独安装、单独运行：不 import 原机器人工程，
不依赖它的模型/对话/托盘/目录结构，也不与旧的 8127 三条 BTC 试运行共享账号、数据
目录或服务名。

服务做四件事：**记账**（份额、净值、分红、赎回）、**收付**（申购收款、赎回付款、通知）、
**交易**（按冻结策略下单与风控）、**运维**（备份、迁移包、健康探测、运营台）。

---

## 1. 定位与边界

项目运行 capital1 / capital2 的份额账簿、收付、交易与本机运营台，策略参数由
`POLICIES` 冻结。源代码和依赖已独立打包，原项目与旧 8127 试运行保持各自的数据目录。
运营台监听 `127.0.0.1`，远程访问经 SSH 隧道；站点转账、消息和模拟交易受 `live` 闸门控制。
断网期间的站内通知会保留，恢复后补发。

支持[机构控制用户](docs/design/CONTROL_USER.md)：转入免申购费、按月确认本金；其他用户
已确认申购的 5% 手续费划转控制用户，本金留在基金。控制用户私聊 `/check` 查看双基金资金总览。

**硬约束（改动前请先读 [AGENTS.md](AGENTS.md)）**：

| 约束 | 口径 |
| --- | --- |
| 金额 | 整数 `1e-4` 单位（`MONEY_SCALE=10000`），**禁止 float 累加金额** |
| 份额 | 整数 `1e-8` 原子（`SHARE_SCALE=100000000`） |
| 净值 | `Decimal`，按 60 位精度中间计算后量化到 8 位小数 |
| 时区 | 业务日窗口用北京时（`BEIJING`, UTC+8）；K 线时间是交易所真 UTC |
| 单写者 | 打开 SQLite **之前**先取 `data_lock.py` 的 OS 生命周期锁 |
| 幂等 | 业务幂等键跟着业务走；未知结果先对账，绝不盲目重放有副作用的请求 |
| 密钥 | 只来自环境或数据目录凭据库，永不进仓库、日志、迁移包 |
| 资金规则 | v0.3 与 2026-10-04 控制用户补充规则；策略参数保持冻结 |

---

## 2. 模块架构

```
                         ┌───────────────────────────┐
  浏览器 / SSH 隧道 ───▶ │ web.py   运营台（aiohttp） │
                         └─────────────┬─────────────┘
                                       │
                         ┌─────────────▼─────────────┐
                         │ runtime.py  FundService    │
                         │ 装配 · 4 秒主循环 · 备份    │
                         └──┬─────┬─────┬─────┬───────┘
        ┌───────────────────┘     │     │     └────────────────────┐
┌───────▼────────┐ ┌──────────────▼┐ ┌──▼──────────────┐ ┌─────────▼─────────┐
│ commands.py    │ │ payments.py   │ │ trader.py       │ │ operations.py     │
│ 私聊五命令      │ │ 收款轮询/付款  │ │ 风控与下单       │ │ 备份/迁移/健康/日志 │
│                │ │ 通知 outbox    │ │（用 strategy.py）│ │                   │
└───────┬────────┘ └──────┬────────┘ └──┬──────────────┘ └─────────┬─────────┘
        │                 │             │                          │
        └─────────────────┴──────┬──────┴──────────────────────────┘
                                 │
                    ┌────────────▼────────────┐    ┌────────────────────┐
                    │ ledger.py  FundLedger   │    │ data_lock.py       │
                    │ 份额/分红/赎回/预留/结算  │    │ OS 生命周期单写者锁 │
                    └────────────┬────────────┘    └────────────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │ store.py  SQLite 单写者  │
                    │ records · audit_events  │
                    └─────────────────────────┘

  外部世界：client.py ──▶ raricy.com（登录 / 流水 / 转账 / 私聊 / 练手盘）
            由 adapters.py 把站点数值转成交易侧 DTO，并做 live 闸门
  支持层：  contracts.py（精度与冻结策略）· chat_models.py、site_protocol.py、hourly_signals.py、safe_logging.py
```

| 模块 | 职责 | 关键契约 |
| --- | --- | --- |
| `src/raricy_capital/__main__.py` | 独立入口：解析参数、取数据锁、装配配置、起服务或停机备份 | `--config / --data-dir / --port / --live / --backup` |
| `bootstrap_logging.py` | 启动失败日志：缺依赖、配置错误、锁冲突与服务启动异常 | 标准库独立记录，脱敏 JSONL，1 MiB 加 3 份轮转 |
| `config.py` | `FundConfig` 加载与校验；`CredentialVault`（Fernet）读写站点凭据 | 非回环 host、越界端口、未知字段一律拒绝启动 |
| `contracts.py` | 金额/份额精度、`FundError` 稳定错误码、北京时间、冻结的 `FundPolicy` / `POLICIES` | `money_units()` / `money_text()` / `now_ms()` |
| `store.py` | SQLite 单写者存储：`records` 键值命名空间、`audit_events`、事务上下文 | `transaction()` / `get/put/claim` / `backup()` |
| `ledger.py` | 记账核心：净值、份额、申购到账、赎回预留、分红复投、月末与紧急结算 | 所有状态变更在一个事务内落盘，外部副作用只在其后调度 |
| `client.py` | 站点 HTTP 客户端：登录、余额、流水、转账、私聊、图床、练手盘、支付链接与二维码 | 会话只存内存；错误分类带 `retryable` / `reconcile` |
| `adapters.py` | 站点数值 ↔ 交易侧 DTO 的桥接，并叠加 `live` 与各类 hold 闸门 | `buy` 在非 live 或存在 hold 时抛 `new_entries_disabled` |
| `commands.py` | 私聊命令处理：`/check` `/subscription` `/redemption` `/emergency` `/help`（含 `cancel`） | 身份只认私聊 `author.id`；消息 ID 持久去重后再动账 |
| `payments.py` | 收款游标轮询、付款重试、`NoticeOutbox` 持久通知箱 | 付款永远用同一幂等键重发；`None` 响应不算送达 |
| `trader.py` | 实盘交易运行时：账户对账、净值标记、风控闸门、下单与结算确认 | 意图先落盘再发请求；未知买入不对账不放行新开仓 |
| `strategy.py` | 纯函数策略规则：仓位公式、止损价、冷却、ER12 亏损门、信号新鲜度 | 无 I/O、不改调用方状态 |
| `operations.py` | 运维面：备份轮转、迁移包导出/校验/恢复、健康探测、滚动脱敏日志 | 迁移包白名单成员 + 逐成员 sha256；解包不用 `extractall` |
| `runtime.py` | `FundService`：装配 store/ledger/vault/clients/workers，跑主循环 | 每个 tick 内串行；失败按类别记事件而不是抛出循环 |
| `web.py` | 运营台 HTTP 层：路由、鉴权、请求守卫、响应脱敏 | 见下文「鉴权」 |
| `static/` | 运营台前端页面（净值曲线、持仓人、事件与订单面板） | 纯静态资源，随包分发 |
| `data_lock.py` | 跨进程单写者：Windows `msvcrt.locking` / POSIX `flock` 的 OS 生命周期锁 | 锁随进程退出（含崩溃）由内核释放，没有陈旧锁 |
| `chat_models.py` 等四个支持模块 | 支持层：`chat_models.py`（站内消息 DTO）、`site_protocol.py`（练手盘页面解析 + 站点时间换算）、`hourly_signals.py`（`HourSignal` / `closed_signal` / `payout_units` / `HOUR` / `DAY` / `FEE`）、`safe_logging.py`（安全堆栈） | 入口 `__main__` 直接导入 `data_lock`；其它资金模块按需从上述同级模块导入 |

**数据流要点**：一个 tick 里先处理私聊命令，再收付（收款与预留现金在风控决策前可见），
然后准备流动性、跑交易、做结算；任一环失败都会让本次 tick 判定为不健康并记账，但不会
把已经落盘的意图丢掉。

---

## 3. 独立安装与入口

需要 **Python 3.12 或以上**。包内不依赖 `raricy_bot`、OpenAI、MCP 或旧仓库的任何文件。

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .[dev]
```

**主入口**（唯一正式入口，信号处理、OS 数据锁与装配都在这里）：

```bash
python -m raricy_capital --config packaging/funds/config.example.yaml
```

- 默认绑定 `127.0.0.1:8137`，默认数据目录 `data/capital_funds`，默认 `live=false`；
- 常用开关：`--data-dir`、`--port`、`--live`（真实站点私聊、转账与模拟交易的总闸）、
  `--backup`（**停机状态**下的一次性备份后退出）；
- 依赖齐全时也可用控制台脚本 `raricy-capital`，参数与上面完全一致。

**运维工具**（启动薄封装 + 停机备份、迁移与预检）：

```bash
python tools/run_capital_service.py --config packaging/funds/config.example.yaml check
python tools/run_capital_service.py --data-dir data/capital_funds status
python tools/run_capital_service.py --data-dir data/capital_funds backup
python tools/run_capital_service.py --data-dir data/capital_funds list-backups
python tools/run_capital_service.py --data-dir data/capital_funds export
python tools/run_capital_service.py --data-dir data/capital_funds verify <bundle.zip>
python tools/run_capital_service.py --data-dir data/capital_funds restore <bundle.zip>
```

`serve` 子命令只是把请求**原样转交**给根入口，不自建入口也不改写它的参数契约。
凡是会打开 SQLite 的动作（`backup` / `export` / `restore`）都先取**同一把** OS 锁；
服务在线时以 `service_running` 明确拒绝，退出码 `3`。

安装路径需要说明：仓库里另有一份 Linux systemd 与 Windows 计划任务的部署物料
（`packaging/funds/`），**但它们尚未在真实 Linux 服务器或真实账户上验收过**，本
README 不声称已完成部署。运维侧请读 [DEPLOYMENT.md](docs/usage/DEPLOYMENT.md)。

---

## 4. 文档

完整导航见 [文档索引](docs/README.md)，历史记录见 [归档索引](docs/ARCHIVE.md)。

| 文档 | 读者 | 内容 |
| --- | --- | --- |
| [USAGE.md](docs/usage/USAGE.md) | 开发者 | 环境、目录结构、契约、配置、鉴权、针对性测试与扩展策略的前置条件 |
| [DEPLOYMENT.md](docs/usage/DEPLOYMENT.md) | 运维 | Linux systemd / Windows 计划任务的部署包、安装与回滚物料 |
| [早期前端更新（历史）](docs/archive/2026-10-04/FRONTEND_UPDATE.md) | 运维 | 当时的界面验证与静态替换记录 |
| [未认领款操作手册](docs/usage/UNCLAIMED_REVIEW.md) | 管理人 | 逐笔核对、关联申购、原路退款、进度与审计 |
| [人工核对设计](docs/design/UNCLAIMED_REVIEW.md) | 开发者 | 匹配约束、接口、并发与退款对账 |
| [GUIDE.md](docs/usage/GUIDE.md) | 基金方与持有人 | 产品规则：费用、窗口、预留份额、二维码与迟延退款、豁免与撤回 |
| [INTRODUCTION.md](docs/funds/INTRODUCTION.md) | 研究/评审 | 两只策略的研究口径、历史图表与压力情形分析 |
| [AGENTS.md](AGENTS.md) | 一切自动化协作方 | 工作约定：偏好、文件所有权、最小改动与不可逾越的资金安全红线 |
| [CLAUDE.md](CLAUDE.md) | Claude Code | 上述通用约定 + 本仓库的具体工作流 |
| [推广文案](promotion/推广文案.md) | 网站用户 | 两条基金的简洁图文介绍，含历史回测收益、回撤和口径 |
| [研究材料](docs/materials/research/) | 研究/评审 | 研究原始材料与图表来源说明；配图在 `docs/materials/assets/` |

---

## 5. 当前状态与已知差距

**已经可用且可在本机验证的部分**：拆分后的独立导入与打包、账务不变量、备份与迁移包
的闭环与安全边界、OS 单写者锁、健康探测与脱敏日志、私聊命令的处理与去重、交易侧
的意图持久化与对账路径。针对性测试都在 `tests/` 下，运行方式见 [USAGE.md](docs/usage/USAGE.md)。

**尚未完成、不得对外声称已完成的部分**：

- 未在真实 Linux 服务器上验收 systemd unit 的组合（`Restart=always`、
  `ProtectSystem=strict`、`ReadWritePaths`）；
- 未注册任何 Windows 计划任务，只提供脚本；
- 独立告警出口（`alert_sender` 钩子）没有任何部署路径会配置它，断网期间的站内通知
  只能在恢复后补发；
- 凭据库（`credentials.enc` + `credential.key`）的迁移是**人工**的成对转移，没有自动化；
- 网络文件系统（NFS/SMB）上的文件锁语义未测试；
- 真实账户的端到端收付与长期备份的体积/耗时未压测。

> 任何一项未打勾之前，都不要对外宣称对应目标已完成。本轮交付的是**代码与本机可验证
> 的部分**，不是生产验收结论。

验证命令见 [USAGE.md](docs/usage/USAGE.md)；本机检查与真实站点部署验收分别记录。

## 6. 本机验证记录

2026-10-04，在本项目独立 `.venv` 中运行 `tests/`，**190 项通过**。
独立 wheel 构建成功并包含运营台静态资源；入口和 Linux 安装脚本语法检查通过。
本轮补充启动登录重试、积压月份按序结算、挂起申请撤回与通知金额单位修正。
空账户预览验证了鉴权、请求来源保护、循环心跳及在线备份，外部写操作保持关闭。
真实账户收付、交易和 Linux 服务部署仍待目标环境验收，详见 [部署指南](docs/usage/DEPLOYMENT.md)。

### 人工核对功能补充验证（2026-10-04）

Claude Code 子代理完成账务、接口与页面分工，并独立审查资金和并发边界。集成后针对性回归
**202 项通过**，覆盖人工核对、既有账务、命令、服务、客户端和管理接口。文档本地链接检查通过。
真实收付与服务器部署仍未验收；现行操作见 [人工核对手册](docs/usage/UNCLAIMED_REVIEW.md)。


### 控制用户功能补充验证（2026-10-04）

针对性回归 **228 项通过**，其中控制用户新增测试 24 项；覆盖机构本金免收费与月末发行、
普通申购费发行后划转、撤回全额退款、现金保护、未知付款对账、私聊权限及账号隔离。
配置支持 YAML 与环境变量，实际控制用户 ID 只保存在 Git 忽略的本机配置。
本轮未启动或重启真实服务，未执行真实转账；服务器升级与站点端到端验收仍待完成。
规则与配置见[CONTROL_USER.md](docs/design/CONTROL_USER.md)。
