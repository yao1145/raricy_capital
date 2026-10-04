# 双基金服务部署手册（DEPLOYMENT）

本手册面向**运维与管理员**，描述如何在 Linux 服务器与 Windows 主机上安装、配置、
运行、备份、迁移和验收 `src/raricy_capital` 这套 capital1 / capital2 资金服务。

- 只讲**部署与运维**。产品规则、费用与窗口见 [`GUIDE.md`](GUIDE.md)；开发者环境与
  模块结构见 [`USAGE.md`](USAGE.md)；项目背景见 [`INTRODUCTION.md`](../funds/INTRODUCTION.md)；
  仓库总览见 [`README.md`](../../README.md)。
- 部署物料在 [`packaging/funds/`](../../packaging/funds/README.md)。
- 服务只监听 **127.0.0.1**，远程访问一律经 SSH 隧道；默认 **`live: false`（只读预览）**，
  真实下单 / 转账 / 私聊必须显式开启。

> 本手册按代码契约编写。仓库里**没有**保留任何真实 Linux 安装、Windows 任务注册、
> 外网请求或真实账号操作的执行记录；标为「待人工验收」的条目必须在目标机上执行后
> 才能打勾，见 §15。

---

## 1. 前提条件

### Linux（服务端）

| 项 | 要求 |
| --- | --- |
| 系统 | 带 systemd 的常见发行版；`bash`；`sudo`；有 root 权限 |
| Python | **3.12 或以上**（硬性要求）。`python3` 版本不够时用 `FUNDS_PYTHON=/usr/bin/python3.12` 指定 |
| Python 模块 | `python3-venv`（安装脚本会建虚拟环境）；`pip` 能访问 PyPI 或内网镜像 |
| 端口 | 本机回环 `127.0.0.1:8137` 未被占用；**不需要**对外开放 |
| 磁盘 | 数据目录 + 备份目录（默认每小时一份、保留 72 份）够用 |
| 其他 | 需要远程访问时服务器上有 `sshd`；`sqlite3` 命令行仅排障时可选 |

安装脚本会先预检解释器，版本低于 3.12 直接报错退出：

```bash
python3 -c "import sys; print(sys.version)"        # 期望 3.12+
sudo bash packaging/funds/install.sh               # 在仓库根目录执行
```

### Windows（本机 / 单机运行）

| 项 | 要求 |
| --- | --- |
| Python | **3.12 或以上**，建议在仓库根建 `.venv` 并 `pip install .` |
| 组件 | PowerShell 5.1+、`wscript.exe`（系统自带）、任务计划程序 |
| 数据目录 | 默认 `<仓库根>\data\capital_funds`，例：`D:\Study\Code\raricy_capital\data\capital_funds` |

计划任务注册脚本同样会预检 Python 版本（见 §12）。

---

## 2. 安装（Linux）

在**仓库根目录**以 root 运行：

```bash
sudo bash packaging/funds/install.sh              # 只安装，不启用、不启动
sudo bash packaging/funds/install.sh --help       # 查看脚本自带说明
```

脚本按**显式来源白名单**同步代码，绝不整体同步仓库根、也绝不 `rsync --delete`：

| 来源 | 说明 |
| --- | --- |
| `src/` | 整个 `raricy_capital` 包（含控制台静态页） |
| `pyproject.toml` | 根项目元数据与依赖 |
| `packaging/funds/` | 本目录的部署物料 |
| `tools/run_capital_service.py` | 运维 CLI |
| 根公开文档 | `README.md`（缺失只警告） |
| `docs/**` | 使用、部署、投资者指南、研究报告、规则与图表（缺失只警告） |

**不会**复制：`.build/`、`data/`、`.venv/`、`.env`、`config*.yaml`、凭据文件、原始旧
仓库，以及给编码代理用的 `AGENTS.md` / `CLAUDE.md`。已有的 `.venv` 会被保留。

依赖安装走根 `pyproject.toml` 的**普通 `dependencies`**（httpx、PyYAML、aiohttp、
cryptography、`qrcode[pil]`）——本项目**没有** `[funds]` extra，脚本执行的是
`pip install <安装目录>`，不带 extra。内网/离线环境请先设置 `PIP_INDEX_URL` 或
`PIP_FIND_LINKS`。

安装完成后脚本会打印后续步骤；此时**服务尚未启用**，也没有任何真实凭据。脚本是幂等
的，可以反复运行。

主要落点：

| 路径 | 用途 |
| --- | --- |
| `/opt/raricy-funds` | 代码与 `.venv` |
| `/etc/raricy-funds/funds.yaml` | 服务配置（0640 root:raricy-funds） |
| `/etc/raricy-funds/funds.env` | 凭据环境文件（占位符，需人工填写） |
| `/var/lib/raricy-funds` | 数据目录（0700，raricy-funds） |
| `/etc/systemd/system/raricy-funds.service` | systemd 服务单元 |

`raricy-funds-backup.service` / `.timer` **不会被安装**（原因见 §10）。

---

## 3. 配置

配置由 `src/raricy_capital/config.py` 的 `FundConfig` 加载与校验。**本文件是 YAML，
不是 JSON**：样例里的 `#` 注释在 JSON 里非法，整份样例按 JSON 解析会直接失败（加载器
用 `yaml.safe_load`，它恰好也接受无注释的 JSON 子集，但请统一按 YAML 编辑）。

样例见 [`packaging/funds/config.example.yaml`](../../packaging/funds/config.example.yaml)；
Linux 安装脚本会把它复制为 `/etc/raricy-funds/funds.yaml` 并把 `data_dir` 改写为
`/var/lib/raricy-funds`。

| 键 | 默认 | 约束 / 含义 |
| --- | --- | --- |
| `data_dir` | `data/capital_funds` | 数据目录；数据库、备份、导出、日志、令牌都在其下 |
| `host` | `127.0.0.1` | **只能是回环**，否则 `loopback_required` |
| `port` | `8137` | 1024–65535 |
| `site_url` | `https://raricy.com` | 必须是 `https`，不得带凭据 / 查询串 / 片段，否则 `invalid_site_url` |
| `live` | `false` | 外部写总闸，见 §5 |
| `control_user_id` | 空 | 两基金共用的控制用户站点 ID；必须与两交易账号不同 |
| `tick_seconds` | `4` | 主循环与健康探测间隔，1–60（离线判定是**敏感**的） |
| `backup_interval_seconds` | `3600` | 服务内部备份周期，整数且 ≥ 60 |
| `backup_retention` | `72` | 备份保留份数，1–10000 |
| `outage_failure_threshold` | `3` | 连续多少次探测失败判定断线 |
| `log_max_bytes` | `10485760` | 单段 JSONL 日志上限，超出轮转 |
| `log_backup_count` | `10` | 保留日志段数 |

出现**未知字段**、`fund_ids` 与冻结名单不一致、或试图改写 `policies` 都会使服务以
`invalid_configuration` / `invalid_fund_configuration` / `policies_are_frozen` 拒绝启动。
旧字段名 `backup_keep`、`log_keep`、`health_interval_seconds`、
`health_failure_threshold` 仍被接受并映射到上表的新名字。

预检（**不打开数据库、不取锁、不启服务**，只校验配置）：

```bash
python tools/run_capital_service.py --config /etc/raricy-funds/funds.yaml check
```

服务入口与开关（`--config` / `--data-dir` / `--port` / `--live` / `--backup`）：

```bash
python -m raricy_capital --config /etc/raricy-funds/funds.yaml          # 前台启动
python -m raricy_capital --config /etc/raricy-funds/funds.yaml --backup # 停机一次性备份
```

配置加载时会确保 `data_dir` 存在，并在没有 `FUNDS_CONTROL_TOKEN` 时按需生成
`admin.token`，因此 `check` 也可能创建该令牌文件——它属于凭据，见 §4。

---

### 3.1 启用控制用户

在目标机私有 `funds.yaml` 添加 `control_user_id: "实际站点用户ID"`；也可在服务环境
填写 `FUNDS_CONTROL_USER_ID`（只有 YAML 留空时采用）。该 ID 不是管理令牌；不需要
控制用户的密码。两个基金继续各用自己的登录账号。账号冲突会报
`control_user_cannot_be_fund_account`，不能忽略或换显示名绕过。

本机已准备 Git 忽略的 `config.local.control-user.yaml`，保持 `live: false`。
它是配置模板，不会改写现有服务的启动配置；服务器须另行填写自身数据目录与该用户 ID。
升级整包后按本手册的单写者流程重启服务；仅替换页面无法启用后端收付规则。

启用 live 前核对旧的已发行申购费：仍在账且尚未排队的费用会自动补建机构付款队列，
已排队费用保持原收款人和业务键。历史本金、份额与未认领款不自动重新归属。
确认本金不被划走，发行前本金和费用仍可退款；验收一笔机构入金、一笔普通申购费划转
及控制用户私聊 `/check`。具体口径见[控制用户设计](../design/CONTROL_USER.md)。
本机测试之外的真实收付与目标服务器部署仍待验收。

## 4. 凭据与密钥（永不进仓库）

凭据**只**来自运行环境或数据目录里的凭据库，绝不写进 unit、任务、脚本、YAML 或迁移包。

| 凭据 | 来源 | 说明 |
| --- | --- | --- |
| 站点账号 | `FUNDS_CAPITAL1_USERNAME` / `_PASSWORD`、`FUNDS_CAPITAL2_USERNAME` / `_PASSWORD` | 每只基金一个独立账号；用户名以 `changeme` 开头会被忽略；启动时校验两个账号 ID 不同（`fund_accounts_must_differ`），账号变更会被 `account_identity_changed` 拒绝 |
| 凭据库 | `<数据目录>/credentials.enc` + `credential.key` | Fernet 加密；可用 `FUNDS_CREDENTIAL_KEY`（base64）注入密钥替代 key 文件；两者必须**成对**保管 |
| 控制令牌 | `FUNDS_CONTROL_TOKEN`，或首次启动生成的 `<数据目录>/admin.token`（0600） | 二选一；短于 24 位会以 `weak_control_token` 拒绝启动；`admin.token` 是独立凭据，不随迁移包迁移 |

- **Linux**：`/etc/raricy-funds/funds.env`，`chown root:raricy-funds`，`chmod 0600`
  （脚本初始放置为 0640），由 unit 的 `EnvironmentFile=` 注入。样例见
  [`packaging/funds/funds.env.example`](../../packaging/funds/funds.env.example)。
- **Windows**：当前用户环境变量或独立凭据文件，切勿放进计划任务命令行。
- 日志与事件详情会**按键名整体剔除**敏感字段（`password`、`token`、`cookie`、
  `auth`、`credential`、`session`、`private`、`api_key` 等）。
- 管理令牌只在本机或 SSH 隧道页面里输入，站点密码只在各基金登录表单里输入；两者都
  不要发到聊天里。

---

## 5. 只读优先，live 必须显式开启

`live` 是所有**外部写**的总闸：非 live 时不下真实订单、不发起转账、不发私聊，只做本地
账务、对账与页面预览；需要外部写的接口会以 `live_required` / `live_account_required`
拒绝。

推荐流程：

1. 先以默认 `live: false` 安装并启动，登录各基金账号，核对账面余额、份额与事件；
2. 确认无误后，再**显式**打开：把 `/etc/raricy-funds/funds.yaml` 的 `live` 改成 `true`，
   或在启动时加 `--live`（CLI 开关会强制置真）；
3. 打开后先用管理控制台观察一轮完整的健康探测与对账，再让策略真正开始工作；
4. 任何时候想回到只读，改回 `false` 并重启服务即可。

只读模式下仍然需要凭据（要登录站点读取余额与行情），但这些凭据仅用于读取。

---

## 6. systemd 运行与单写者

单元文件：[`packaging/funds/raricy-funds.service`](../../packaging/funds/raricy-funds.service)。
要点：`User=raricy-funds`、`EnvironmentFile=-/etc/raricy-funds/funds.env`、
`Environment=FUNDS_BOOTSTRAP_LOG_DIR=/var/lib/raricy-funds/logs`、
`ExecStart=.../python -m raricy_capital --config /etc/raricy-funds/funds.yaml`、
`Restart=always`、`RestartSec=5`、`KillSignal=SIGTERM`、`TimeoutStopSec=45`，并以
`ProtectSystem=strict` + `ReadWritePaths=/var/lib/raricy-funds` 限制可写范围。

**重启策略是有意无限重试**：unit 设 `StartLimitIntervalSec=0`（关闭启动速率限制）配合
`Restart=always`，崩溃、被 OOM、被信号打断都会按 `RestartSec=5` 重新拉起，直到站点恢复
连通。**不要**加 `StartLimitBurst`——它属于 `[Unit]` 段，写在 `[Service]` 里无效；即便
写对位置，有限次数上限也可能在站点恢复连通前把服务永久停摆，这与「保证最终恢复」相悖。
启动失败原因见 §9 的 `bootstrap.jsonl`。

```bash
sudo systemctl enable --now raricy-funds.service   # 安装脚本的 --enable 等价于这条
systemctl status raricy-funds.service
journalctl -u raricy-funds.service -f              # 启动信息与致命错误
sudo systemctl stop raricy-funds.service           # SIGTERM，给足停止时间
```

**单写者**：入口在打开 SQLite **之前**取得数据目录上的 OS 生命周期锁
（`.raricy-data.lock`，POSIX `flock`）。锁随进程退出或崩溃由内核释放，**不存在**需要
人工清理的陈旧锁，也不能靠改 pid 夺锁。

- 重复启动同一数据目录：第二个进程以 `data_in_use` 失败退出（systemd 会重启它，但
  它始终拿不到锁）。
- 离线运维动作（`backup` / `export` / `restore`）取**同一把**锁；服务在线时以
  `service_running` 拒绝，绝不并发写。
- 不要把同一份数据目录放到 NFS/SMB 上共享，也不要在 Linux 与 Windows 上同时运行；
  网络文件系统上的锁语义未经验证。

---

## 7. 访问与控制台（SSH 回环隧道）

服务端不监听公网。运维在**本机**开一条 SSH 回环隧道：

```bash
ssh -N -L 127.0.0.1:8137:127.0.0.1:8137 \
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -i ~/.ssh/raricy_funds_tunnel funds-operator@your-server-host
```

长期运行可用 [`packaging/funds/raricy-funds-tunnel.service`](../../packaging/funds/raricy-funds-tunnel.service)：
它是 **systemd user 单元**，装在**运维/管理员本机**（不是服务器），以当前登录用户身份运行。
因此它**不写 `User=`，也不使用 `%i`**——`%i` 只对模板单元 `foo@.service` 有定义，本文件名
不是模板；`SSH_KEY` 用 `%h/.ssh/raricy_funds_tunnel`（当前用户家目录）而不是 `/home/%i/...`。

```bash
mkdir -p ~/.config/systemd/user
install -m 0644 packaging/funds/raricy-funds-tunnel.service \
    ~/.config/systemd/user/raricy-funds-tunnel.service
# 编辑该文件里的占位符 SERVER=funds-operator@your-server-host（不要臆造服务器 IP）；
# 专用密钥 ~/.ssh/raricy_funds_tunnel 由运维预先准备，unit 不会自动生成新密钥或授权。
systemctl --user daemon-reload
systemctl --user enable --now raricy-funds-tunnel.service
systemctl --user status raricy-funds-tunnel.service
```

unit 保留了 `BatchMode=yes`、`ExitOnForwardFailure=yes` 与 `ServerAliveInterval=30` 等
保活/快速失败选项。浏览器访问 `http://127.0.0.1:8137/`。

- **Windows 运维机**：没有 `systemctl --user`，直接在 PowerShell / cmd 里跑上面的
  `ssh -N -L ...` 命令（可做成快捷方式或登录脚本），隧道语义完全相同。
- **服务器上的资金服务单元是另一套**：`raricy-funds.service` 是服务器上的 **system** 级
  unit（`User=raricy-funds`），与本机这条 user 隧道互不安装在同一台机上，不要混装。

控制台自身的防护：请求必须带回环 `Host` 头（否则 `403 forbidden_host`）；所有写方法
必须带**同源** `Origin` 与 `Content-Type: application/json`（否则 `403 forbidden_origin`
/ `415 unsupported_media_type`）；`/api/*` 里除会话登录 `POST /api/login` 与登出
`POST /api/logout` 外都要控制令牌（登录后用 `fund_session` 会话 cookie，或直接
`Authorization: Bearer <令牌>`；各基金的站点登录 `/api/funds/{fund_id}/login` 也要令牌）。

用令牌触发一次已认证的备份：

```bash
curl -sS -X POST http://127.0.0.1:8137/api/backup \
  -H "Origin: http://127.0.0.1:8137" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $FUNDS_CONTROL_TOKEN" \
  -d '{}'
```

---

## 8. 健康探测与断网行为

服务在主循环里每隔 `tick_seconds`（默认 4 秒）探测一次站点：连续
`outage_failure_threshold`（默认 3）次失败即判定**断线**（约 12 秒），一次成功即
**恢复**。状态持久化在数据目录的 `ops` 命名空间。

状态跃迁时写事件并排队通知：断线写 `ops.network_down`，恢复写
`ops.network_recovered`（含离线时长），两者都按基金各入队一条 `notices` 记录
（`user_id` 为空 = 按持有人展开）。

**必须说清楚的实情**：断网期间无法经同一条链路即时发出站内消息，通知只会停在
`queued`，等网络恢复后由消息 worker 补发。代码里有一个**可选的** `alert_sender` 钩子
作为独立告警出口，但**当前没有任何部署路径会配置它**，环境文件也不再提供对应变量——
不要向持有人承诺「断网会立刻收到告警」。

查看运维状态（不打开数据库、不取锁）：

```bash
python tools/run_capital_service.py --data-dir data/capital_funds status
```

输出包含 `network` / `healthy` / `consecutive_failures` / `outage_ms` /
`writer_active` / `backup_count` / `latest_backup` / `health_interval_seconds` /
`updated_ms`。管理控制台的 `/api/status` 提供同一份信息。

---

## 9. 日志与脱敏

- **滚动 JSONL**：`<数据目录>/logs/funds-ops.jsonl`，按大小轮转为 `.1` → `.2` …，
  最多保留 `log_backup_count`（默认 10）段，单段上限 `log_max_bytes`（默认 10 MiB）。
- **每行字段**：`ts`（UTC）、`event`、`event_id`、`fund_id`、`level`、`details`。
- **持久事件**：同一事件同时写入 SQLite 的 `audit_events`，带稳定事件 ID
  （`evt-…` 或由幂等键派生），便于用 `/api/events` 或 `record_event` 对账。
- **脱敏**：敏感键名整体替换为 `[redacted]`，字符串截断，异常只记录**安全堆栈**
  （`模块.函数:行号`）与失败类别，不含源码行、异常正文、密码或 Cookie。
- **进程级输出**：启动信息与致命错误走 stdout/stderr，用
  `journalctl -u raricy-funds.service` 查看（Linux）。
- **启动期致命日志**：`<数据目录>/logs/bootstrap.jsonl`。入口在载入配置、取数据锁或
  绑定端口失败时写这一份，**早于**正常的 `funds-ops.jsonl`，因此即使配置还没读出来
  也有落点。默认目标：有 `--data-dir` 用它，否则仓库默认 `data/capital_funds/logs`；
  环境变量 `FUNDS_BOOTSTRAP_LOG_DIR` **总是优先**（Linux unit 已设为
  `/var/lib/raricy-funds/logs`，用于配置加载失败、`data_dir` 未知的场合）；配置合法后
  改用实际 `config.data_dir/logs`（除非设了该环境变量）。
  - 轮转有界：单段 1 MiB，保留 3 个备份（共 4 段），文件权限 `0600`（POSIX）。
  - 每行 JSONL 含 `ts` / `level` / `phase`（`import` / `config` / `lock` / `serve` /
    `backup`）/ 稳定 `code` / `exit_code`；已知服务错误只记稳定码，其它异常只记
    **异常类型 + errno + 安全的「文件:函数:行」**。
  - **绝不**写入 `str(异常)`、源码行文本、请求正文、环境变量、配置内容或凭据；日志写盘
    失败也不会掩盖原始错误（会尽力在 stderr 输出一行短码）。
  - **Windows 隐藏计划任务**（`pythonw`，`stdout`/`stderr` 为 `None`）默认写仓库
    `data/capital_funds/logs`；若 `data_dir` 在别处，或配置在读出 `data_dir` 前就失败，
    请把 `FUNDS_BOOTSTRAP_LOG_DIR` 设为该数据目录下的 `logs`（用户环境变量）。

---

## 10. 备份与恢复

### 运行中的备份（正常情况）

- 服务**内部**每 `backup_interval_seconds`（默认 3600 秒）自动备份一次；
- 也可在控制台触发**已认证**的 `POST /api/backup`（见 §7）。

备份调用 SQLite **online backup API**，生成一致快照，**绝不**直接复制正在使用的
WAL 运行库。每份备份落在 `<数据目录>/backups/funds-<时间戳>[-label].sqlite3`，并带
`.sha256` 旁车；超过 `backup_retention`（默认 72）份时按新→旧轮转删除（含旁车）。

```bash
python tools/run_capital_service.py --data-dir data/capital_funds list-backups
```

### 停机窗口的一次性备份

```bash
python tools/run_capital_service.py --data-dir data/capital_funds backup --label before-upgrade
python -m raricy_capital --config /etc/raricy-funds/funds.yaml --backup
```

[`raricy-funds-backup.service`](../../packaging/funds/raricy-funds-backup.service) /
[`raricy-funds-backup.timer`](../../packaging/funds/raricy-funds-backup.timer) 是**默认禁用、
且不由安装脚本安装**的停机备份入口（timer 不设 `OnCalendar`，永不自动触发）。**不要**
自建外部每日定时器：那会开第二个写者，与运行中的服务争用同一份 SQLite。

### 迁移包：导出 / 校验 / 恢复

迁移包是 zip，只含固定成员：`manifest.json`、`data/funds.sqlite3`（一致快照）、
`config/public.json`（**脱敏公开配置**）、`README.txt`。每个成员带 `sha256`，整包另带
`.sha256` 旁车。**包内不含任何凭据**。

```bash
# 停服后导出（默认落在 <数据目录>/exports/）
python tools/run_capital_service.py --data-dir data/capital_funds export

# 校验（成员白名单、路径安全、大小上限、压缩比、逐成员 sha256；不打开数据库）
python tools/run_capital_service.py --data-dir data/capital_funds verify <bundle.zip>

# 停服后恢复（默认落在 <数据目录>/restore/<时间戳>/）
python tools/run_capital_service.py --data-dir data/capital_funds restore <bundle.zip>
```

解包安全：不使用 `extract`/`extractall`，逐个成员校验——绝对路径、盘符、反斜杠、`..`、
控制字符、白名单外成员、符号链接、重复成员、超大解压量、过高压缩比任一命中即拒绝
（`bundle_member_invalid` / `bundle_member_disallowed` / `bundle_symlink` /
`bundle_duplicate_member` / `bundle_too_large` / `bundle_ratio` …）；成员 sha256 或整包
旁车不符同样拒绝（`bundle_checksum`）。

`restore` 探测的是**目标**目录上的 OS 锁（不是源目录），目标正在被写入时以
`writer_active` 拒绝；目标非空时需显式 `--force`。

---

## 11. 迁移：Linux ↔ Windows

迁移的对象只有一份 **数据目录**。默认位置：Linux `/var/lib/raricy-funds`，Windows
`<仓库根>\data\capital_funds`（例：`D:\Study\Code\raricy_capital\data\capital_funds`）；
源码/本地运行默认是相对的 `data/capital_funds`。

数据目录里可能包含：

| 文件 | 是否随迁移包走 | 说明 |
| --- | --- | --- |
| `funds.sqlite3` | ✅（作为 `data/funds.sqlite3`） | 账务主库 |
| `backups/`、`exports/`、`restore/` | ❌ | 旧备份/导出件自行决定是否另存 |
| `logs/` | ❌ | 排障用，可另存 |
| `credentials.enc` + `credential.key` | ❌（**另外安全通道**） | 必须成对转移，缺一不可 |
| `admin.token` | ❌ | 可单独安全转移，或让新机首次启动重新生成 |

步骤：

1. 在旧机**停服**（`systemctl stop raricy-funds.service` 或结束任务），确认没有写者：
   `python tools/run_capital_service.py --data-dir <旧数据目录> status` 里
   `writer_active` 为假；
2. `export` 迁移包并 `verify`，记录包 sha256；
3. 通过**独立安全通道**单独传送凭据：`credentials.enc` 与 `credential.key` 成对转移，
   **或者**干脆不迁移、到新机后重新登录各基金账号（登录会把凭据写入新机凭据库）；
   `admin.token` / `FUNDS_CONTROL_TOKEN` 另行处理；
4. 在新机完成安装（§2 / §12），确认服务**未启动**；
5. 在新机 `restore <bundle.zip>`，核对目标目录里的 `config/public.json` 与清单；
6. 把恢复出来的 `data/funds.sqlite3` 放入新机数据目录，按 §4 配好凭据，按 §3 核对
   `host`/`port`/`live`；
7. 先以 `live: false` 启动，用控制台核对两只基金的净值、份额、持有人与最新事件，
   再做一次内部备份；确认无误后才考虑按 §5 打开 `live`。

跨平台注意：Windows 与 Linux 的数据目录路径不同，`funds.yaml` 里的 `data_dir` 要
改成新机的绝对路径；备份文件名只含时间戳与安全字符，可跨平台使用。**不要**让两台
主机同时持有同一份数据目录。

---

## 12. Windows 隐藏计划任务

```powershell
# 注册（默认不启动；只注册服务任务，不注册备份任务）
powershell -ExecutionPolicy Bypass -File packaging\funds\windows\install_funds_tasks.ps1 `
    -RepoRoot D:\Study\Code\raricy_capital `
    -DataDir  D:\Study\Code\raricy_capital\data\capital_funds `
    -ConfigPath D:\Study\Code\raricy_capital\config.funds.yaml

# 只看将执行的动作：不生成配置、不注销/注册任务
powershell -File packaging\funds\windows\install_funds_tasks.ps1 -WhatIf

# 移除（不动数据与凭据）
powershell -File packaging\funds\windows\uninstall_funds_tasks.ps1
```

- 注册的动作是 `wscript.exe` 调用
  [`windows/run_funds_hidden.vbs`](../../packaging/funds/windows/run_funds_hidden.vbs)：优先用
  `<仓库根>\.venv\Scripts\pythonw.exe`，退回 `python.exe`，以隐藏窗口运行
  `python -m raricy_capital --config <配置>`；启动器等待子进程并回传退出码，任务据此
  在故障时按 1 分钟间隔自动重启（`RestartCount=999`）。
- 触发条件是**登录时**，以当前用户身份运行（SYSTEM 拿不到用户的 `.venv` 和凭据）。
- 首次注册时若 `-ConfigPath` 不存在，会从 `config.example.yaml` 生成一份，并把
  `data_dir` 指向 `-DataDir`（默认 `<仓库根>\data\capital_funds`）。
- **没有每日备份任务**：备份由服务内部每小时执行，或经控制台
  `POST /api/backup` 触发；旧版本注册过的 `RaricyFundsBackup` 会在安装/卸载时清理。
- 凭据仍由当前用户环境变量或独立凭据文件提供，绝不写进任务命令行。

---

## 13. 停止、升级与卸载

```bash
# Linux
sudo systemctl stop raricy-funds.service                  # 正常停止（SIGTERM）
sudo bash packaging/funds/install.sh                      # 升级：重新同步白名单并重装依赖
sudo systemctl restart raricy-funds.service
sudo systemctl disable --now raricy-funds.service         # 取消启用
```

- 升级前先做一次停机备份（§10）；升级脚本幂等，不会删 `.venv`，也不会碰数据目录。
- 卸载只涉及 unit 与代码目录：`disable --now` 后自行决定是否删除 `/opt/raricy-funds`；
  `/var/lib/raricy-funds`（数据库、备份、凭据）与 `/etc/raricy-funds` 应**保留**，删除
  前先确认已备份。Windows 侧用 `uninstall_funds_tasks.ps1`，它同样不动数据与凭据。
- 停止后锁由内核自动释放，没有需要手工清理的锁文件。

---

## 14. 常见拒绝码对照

| 码 | 出现位置 | 处理 |
| --- | --- | --- |
| `loopback_required` | 启动 / `check` | `host` 改回 `127.0.0.1` |
| `invalid_configuration` | 启动 / `check` | 有未知字段或取值越界，对照 §3 |
| `invalid_site_url` | 启动 | `site_url` 必须是干净的 `https` 根地址 |
| `weak_control_token` | 启动 | 控制令牌不足 24 位，换真实令牌 |
| `credentials_required` | 登录 | 用户名/口令为空 |
| `fund_accounts_must_differ` | 登录 | 两只基金必须使用不同的站点账号 |
| `live_required` / `live_account_required` | 结算 / 开启运行 | 需要显式 `live`（§5） |
| `data_in_use` | 启动 | 已有写者，检查是否重复启动 |
| `service_running` | 运维脚本写操作 | 服务在线，先停服再备份/导出/恢复 |
| `writer_active` | 运维脚本 / 恢复 | 目标目录仍有写者 |
| `restore_target_exists` | `restore` | 目标非空，需要 `--force` 或换目录 |
| `bundle_*` | `verify` / `restore` | 迁移包被篡改或格式不符，见 §10 |
| `403 forbidden_host` / `forbidden_origin` | 控制台 | 经隧道用回环地址访问，并带同源 `Origin` |
| `401 unauthorized` | 控制台 `/api/*` | 先 `POST /api/login` 或用 `Bearer` 令牌 |

---

## 15. 人工验收清单

以下是上线前应在目标机上执行的检查。**本手册编写期间没有执行过任何安装、服务注册、
联网或真实账号操作**；未打勾的项目不得对外宣称已完成。

**Linux 服务端**

- [ ] `python3 -c "import sys; ..."` 或 `install.sh` 预检确认 Python ≥ 3.12；
- [ ] `install.sh` 白名单同步结果正确：`/opt/raricy-funds` 下只有 src、pyproject、
      packaging/funds、tools、文档与 docs，**没有** `.build/`、`data/`、`.env`、凭据；
- [ ] `pip install` 成功且**没有**引用任何 `[funds]` extra；
- [ ] `tools/run_capital_service.py --config .../funds.yaml check` 通过；
- [ ] 首次启动按 §5 以 `live: false` 运行，控制台可登录、状态页显示健康；
- [ ] `status` 中 `writer_active` 为真；再启一个实例会被 `data_in_use` 拒绝；
- [ ] 停止服务后，`status` 中 `writer_active` 变为假（锁已由内核释放）；
- [ ] 服务内部自动备份与 `POST /api/backup` 都产出带 `.sha256` 旁车的备份；
- [ ] `export` → `verify` → `restore` 闭环成功，包内无凭据；
- [ ] 服务单元的 `Restart=always` + `StartLimitIntervalSec=0` 生效，且没有启用
      `StartLimitBurst`（`systemctl show raricy-funds.service -p Restart -p StartLimitIntervalSec`）；
- [ ] 故意写坏 `funds.yaml` 触发一次启动失败，`<数据目录>/logs/bootstrap.jsonl` 出现
      含稳定 `code` 的一行且不含异常正文/凭据，服务随后仍按 `RestartSec=5` 重试；
- [ ] SSH 隧道能从运维本机打开 `http://127.0.0.1:8137/`，且服务端口未对外暴露：
      Linux 运维机把 `raricy-funds-tunnel.service` 装成 **user** 单元
      （`systemctl --user enable --now`），Windows 运维机直接跑 `ssh -L` 命令；
- [ ] 一次真实（或演练）的 `live` 打开与关闭，确认非 live 期间外部写确实被拒；
- [ ] 升级演练：停机备份 → 重跑 `install.sh` → 重启 → 账目与事件无差异。

**Windows 主机**

- [ ] `install_funds_tasks.ps1 -WhatIf` 输出计划动作，且**没有**生成配置、注销/注册任务；
- [ ] 正式注册只新增 `RaricyFundsService`，没有备份任务；
- [ ] 隐藏启动器不弹控制台窗口，故障后计划任务按 1 分钟间隔重启；
- [ ] `uninstall_funds_tasks.ps1` 移除任务后数据目录与凭据文件保持不变。

**迁移演练（Linux ↔ Windows 任一方向）**

- [ ] 旧机停服 → 导出并校验迁移包 → 新机恢复 → 先 `live: false` 核对净值与份额 →
      再决定是否打开 `live`；
- [ ] 凭据经独立安全通道成对转移（或在新机重新登录），全程未进迁移包与 Git。


## 人工核对功能升级（2026-10-04）

本版本新增逐笔详情、只读预览、按版本提交、严格关联已有申购单与原路退款队列。
操作见 [人工核对手册](UNCLAIMED_REVIEW.md)，约束见 [人工核对设计](../design/UNCLAIMED_REVIEW.md)。

这是包含后端的更新，旧版三文件静态更新包不足以发布本功能。按本文既有升级流程部署完整代码或
wheel：确认实际服务入口与 Python 环境，停止服务后备份账簿及配置，安装新版本，再启动服务。
不要覆盖账号凭据、管理令牌、配置和数据目录；仅保留一份账簿写入服务。

上线后先检查只读列表、到账字段、候选和预览。正式处理需要 live 总闸开启；确认结果与实际站点
收付流水后再登记验收。排队、余额不足与付款结果未知分别显示，不以队列创建代替付款成功。

本机相关回归 202 项通过；验证使用独立测试数据库与虚构站点客户端。Linux 真实部署和真实账户
端到端验收尚未完成。文档分类调整不改变配置、数据目录、基金规则与策略参数。

隔离浏览器演示已验证：登录框隐藏、关联后净资产与份额不变、退款进度自动刷新为已退款、
修改理由使预览失效、只读模式禁止提交、退出后清空页面、390px 布局无横向溢出。
演示没有访问真实站点，也没有向真实用户转账或发送消息。
