#!/usr/bin/env bash
# capital1 / capital2 独立基金资金服务 —— Linux 安装脚本（systemd）
#
# 只做**安装**：建系统用户与目录、按**显式来源白名单**同步代码、在虚拟环境里安装
# 本包（普通依赖，本项目没有 [funds] extra）、放置 systemd 服务 unit 与初始配置样例。
# 默认**不启用、不启动**任何服务，也不写入任何真实凭据——启用由运维核对凭据后显式
# 执行（见文件末尾的 --enable）。
#
# 幂等、可重复运行：只覆盖白名单里的文件，**绝不** `rsync --delete`（那会连 `.venv`
# 一起删掉），也**绝不**复制仓库根的本机状态（`.build/`、`data/`、`.env`、
# `config*.yaml`、`.venv/`、凭据文件、原始旧仓库等）。
#
# 备份策略：运行中的备份由服务**内部**每小时执行（backup_interval_seconds=3600），或
# 经控制台已认证的 POST /api/backup 触发；**不安装**外部每日备份定时器——那会开第二个
# 写者，与运行中的服务争用同一份 SQLite。
#
# 用法（在仓库根目录，以 root 或 sudo 运行）：sudo bash packaging/funds/install.sh
# 之后放置 /etc/raricy-funds/funds.env，再显式启用：sudo bash packaging/funds/install.sh --enable
set -euo pipefail

APP_USER="raricy-funds"
APP_DIR="/opt/raricy-funds"
DATA_DIR="/var/lib/raricy-funds"
CONF_DIR="/etc/raricy-funds"
UNIT_DIR="/etc/systemd/system"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# 根目录公开文档：逐条显式列出（缺失只警告、不失败）。AGENTS.md / CLAUDE.md 是给
# 编码代理看的元文件，不属于部署物料，因此**不在**白名单里。
ROOT_DOCS=(README.md)

ENABLE=0
PYTHON_BIN="${FUNDS_PYTHON:-python3}"

usage() { sed -n '2,18p' "$0"; }

for arg in "$@"; do
  case "$arg" in
    --enable) ENABLE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$arg（支持 --enable / --help）" >&2; exit 2 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请以 root 或 sudo 运行。" >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "找不到解释器 $PYTHON_BIN；可用 FUNDS_PYTHON=/usr/bin/python3.12 指定。" >&2
  exit 1
fi

# 预检：Python 3.12 是硬性要求（项目 pyproject 的 requires-python）。
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "需要 Python 3.12 或以上；可用 FUNDS_PYTHON=/usr/bin/python3.12 指定解释器。" >&2
  exit 1
fi

echo "==> 创建系统用户与目录"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$DATA_DIR"
install -d -o root -g "$APP_USER" -m 0750 "$CONF_DIR"

echo "==> 按显式来源白名单同步代码到 $APP_DIR（保留 .venv，不复制任何本机状态）"
# 白名单只有五项：src/、pyproject.toml、packaging/funds/、tools/run_capital_service.py、
# 根目录公开文档与 docs/**。绝不整体同步仓库根目录。
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$APP_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$APP_DIR/src" "$APP_DIR/packaging/funds" "$APP_DIR/tools"
cp -a "$REPO_ROOT/src/." "$APP_DIR/src/"
cp -a "$REPO_ROOT/packaging/funds/." "$APP_DIR/packaging/funds/"
install -m 0644 "$REPO_ROOT/pyproject.toml" "$APP_DIR/pyproject.toml"
install -m 0644 "$REPO_ROOT/tools/run_capital_service.py" "$APP_DIR/tools/run_capital_service.py"
for doc in "${ROOT_DOCS[@]}"; do
  if [[ -f "$REPO_ROOT/$doc" ]]; then
    install -m 0644 "$REPO_ROOT/$doc" "$APP_DIR/$doc"
  else
    echo "    跳过缺失的文档：$doc"
  fi
done
if [[ -d "$REPO_ROOT/docs" ]]; then
  install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$APP_DIR/docs"
  cp -a "$REPO_ROOT/docs/." "$APP_DIR/docs/"
else
  echo "    跳过缺失的目录：docs/"
fi
chown -R "$APP_USER:$APP_USER" \
  "$APP_DIR/src" "$APP_DIR/packaging" "$APP_DIR/tools" "$APP_DIR/pyproject.toml"
if [[ -d "$APP_DIR/docs" ]]; then
  chown -R "$APP_USER:$APP_USER" "$APP_DIR/docs"
fi

echo "==> 建立虚拟环境并安装本包（普通依赖）"
# 运行期依赖（httpx、PyYAML、aiohttp、cryptography、qrcode[pil]）都在根 pyproject 的
# 普通 dependencies 里，**没有** [funds] extra，因此这里不带 extra。
# 离线/内网环境请先设置 PIP_INDEX_URL 或 PIP_FIND_LINKS。
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  sudo -u "$APP_USER" "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pip install "$APP_DIR"

echo "==> 安装 systemd 服务 unit（不安装外部备份定时器）"
install -m 0644 "$REPO_ROOT/packaging/funds/raricy-funds.service" "$UNIT_DIR/raricy-funds.service"
systemctl daemon-reload

echo "==> 放置初始服务配置（若尚不存在）"
if [[ ! -f "$CONF_DIR/funds.yaml" ]]; then
  install -o root -g "$APP_USER" -m 0640 \
    "$REPO_ROOT/packaging/funds/config.example.yaml" "$CONF_DIR/funds.yaml"
  # Linux 数据卷固定为 $DATA_DIR；把示例里的相对默认值改写为绝对路径，
  # 以便在 ProtectSystem=strict + ReadWritePaths=$DATA_DIR 下正常写盘。
  sed -i "s|^data_dir:.*|data_dir: $DATA_DIR|" "$CONF_DIR/funds.yaml"
  echo "    已写入 $CONF_DIR/funds.yaml（data_dir=$DATA_DIR；host=127.0.0.1、live=false）。"
fi

echo "==> 放置凭据文件（若尚不存在）"
if [[ ! -f "$CONF_DIR/funds.env" ]]; then
  install -o root -g "$APP_USER" -m 0640 \
    "$REPO_ROOT/packaging/funds/funds.env.example" "$CONF_DIR/funds.env"
  echo "    已写入占位符 $CONF_DIR/funds.env —— 请填写真实凭据，填好后 chmod 0600。"
fi

echo
echo "安装完成。下一步（人工执行）："
echo "  1) 编辑 $CONF_DIR/funds.env 与 $CONF_DIR/funds.yaml（host=127.0.0.1、live=false，密钥只来自环境文件）；"
echo "  2) 预检配置（不打开数据库、不取锁、不启服务）："
echo "     sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/tools/run_capital_service.py --config $CONF_DIR/funds.yaml check"
echo "  3) 启用并启动：sudo bash packaging/funds/install.sh --enable"
echo "  4) 隧道：在**运维本机**把 packaging/funds/raricy-funds-tunnel.service 装成 systemd user 单元"
echo "     （~/.config/systemd/user/ + systemctl --user enable --now）；Windows 运维机直接用 ssh -L。"
echo "  5) 备份：运行中的备份由服务内部每小时执行（backup_interval_seconds=3600），"
echo "     也可在控制台触发已认证的 POST /api/backup；不要用外部定时器开第二个写者。"
echo "  6) 完整要点与人工验收清单见 docs/DEPLOYMENT.md。"

if [[ "$ENABLE" -eq 1 ]]; then
  echo "==> 启用并启动服务"
  systemctl enable --now raricy-funds.service
  systemctl status --no-pager raricy-funds.service || true
else
  echo "（未启用：需要时加 --enable 再跑一次。）"
fi
