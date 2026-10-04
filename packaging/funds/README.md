# Raricy 双基金资金服务 —— 部署包

本目录是 `src/raricy_capital` 的**部署与运维物料**，与旧 8127 三条 BTC 试运行服务
完全独立：不共享账号、不共享数据目录、不共用 unit/任务名。

| 文件 | 用途 |
| --- | --- |
| `raricy-funds.service` | Linux systemd 服务单元：`Restart=always`、`SIGTERM` 关停、最小权限 |
| `raricy-funds-backup.service` / `.timer` | **默认禁用、不由安装脚本安装**的停机窗口一次性备份（见下） |
| `raricy-funds-tunnel.service` | **运维本机**的 SSH 回环隧道（服务端只监听 127.0.0.1） |
| `config.example.yaml` | 服务配置样例（字段与 `FundConfig` 一一对应，只有非敏感项） |
| `funds.env.example` | 运行环境变量样例（只有占位符，绝无真实凭据） |
| `install.sh` | Linux 安装脚本；默认只装不启用，`--enable` 才启用 |
| `windows/run_funds_hidden.vbs` | 无窗口启动器（`pythonw`，不弹控制台） |
| `windows/install_funds_tasks.ps1` | 注册隐藏的 Windows 计划任务（不再注册每日备份） |
| `windows/uninstall_funds_tasks.ps1` | 移除上述任务（并清理旧版本的备份任务） |

服务入口是 `python -m raricy_capital`；运维脚本是 `tools/run_capital_service.py`。
unit / 计划任务 / 用户 / 目录名沿用 `raricy-funds`（旧 8127 用的是另一套名字，两者
互不影响），部署路径与示例根目录一律指 `raricy_capital` 仓库本身。

## 边界与安全约定

- **只监听回环**：服务绑定 `127.0.0.1:8137`，`host` 不是回环会在配置校验时被
  `loopback_required` 拒绝；远程访问一律经 SSH 隧道（`-L`）。
- **只读优先**：`live` 默认 `false`。非 live 时一切外部写（下单、转账、发私聊）都被
  拒绝，服务只做本地账务与预览；确需真实操作时再显式打开（`live: true` 或 CLI
  `--live`），详见 `docs/DEPLOYMENT.md` §5。
- **密钥不进物料**：unit、任务、脚本、迁移包里都不含凭据。Linux 用
  `/etc/raricy-funds/funds.env`（仓库外的 0600/0640 文件）；Windows 用当前用户环境
  变量或独立凭据文件。仓库内只有 `*.example` 占位符。变量一律保留 `FUNDS_` 前缀。
- **单写者**：同一份数据只允许一个进程写。`raricy_capital.__main__` 在打开 SQLite
  **之前**用 `raricy_capital.data_lock.acquire_data_lock` 取得 OS 生命周期锁
  （`.raricy-data.lock`）；锁随进程退出/崩溃由内核释放，没有需要人工清理的陈旧锁。
  离线运维脚本会取**同一把锁**，服务在线时以 `service_running` 拒绝并发写。
- **备份**：走 SQLite online backup API，**绝不**直接复制运行中的 WAL 库。运行中的
  备份由服务内部每小时执行（`backup_interval_seconds: 3600`），或经控制台已认证的
  `POST /api/backup` 触发；外部每日定时器会开第二个写者，已停用、也不由安装脚本安装。
- **安装来源白名单**：`install.sh` 只复制 `src/`、`pyproject.toml`、`packaging/funds/`、
  `tools/run_capital_service.py`、根目录公开文档（README/USAGE/DEPLOYMENT/
  INTRODUCTION/GUIDE）与 `docs/**`；保留已有的 `.venv`，**不**复制 `.build/`、
  `data/`、`.env`、`config*.yaml`、凭据文件，也**不**复制原始旧仓库。
- **依赖**：运行期依赖（httpx、PyYAML、aiohttp、cryptography、`qrcode[pil]`）都是根
  `pyproject.toml` 的普通 `dependencies`，**没有** `[funds]` extra；安装脚本执行
  `pip install <安装目录>`，不带 extra。要求 Python 3.12+，安装前会预检。
- **不要提交**：任何含有真实账号、口令或令牌的派生文件都必须加进 `.gitignore` 并只在
  本机保存。

## 快速步骤（Linux）

1. 复制仓库到服务器，`sudo bash packaging/funds/install.sh`（Python < 3.12 会直接
   报错退出；可用 `FUNDS_PYTHON=/usr/bin/python3.12` 指定解释器）；
2. 填 `/etc/raricy-funds/funds.env`，并按需核对 `/etc/raricy-funds/funds.yaml`
   （`host=127.0.0.1`、`live=false`）；
3. 预检 `tools/run_capital_service.py --config /etc/raricy-funds/funds.yaml check`
   （不打开数据库、不取锁）；
4. `sudo bash packaging/funds/install.sh --enable`；
5. 在运维本机配置 `raricy-funds-tunnel.service`，浏览器访问 `http://127.0.0.1:8137/`。

完整的操作手册、迁移步骤与人工验收清单见 `docs/` 下的 [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md)。
