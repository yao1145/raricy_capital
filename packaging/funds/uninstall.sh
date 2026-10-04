#!/usr/bin/env bash
# Raricy 双基金资金服务 —— Linux 卸载脚本。
# 默认只停用并移除 systemd unit；**不删除**数据目录（除非显式 --purge-data）。
# 凭据文件永不自动删除。
set -euo pipefail

APP_USER="raricy-funds"
APP_DIR="/opt/raricy-funds"
DATA_DIR="/var/lib/raricy-funds"
CONF_DIR="/etc/raricy-funds"
UNIT_DIR="/etc/systemd/system"

PURGE_DATA=0
for arg in "$@"; do
  case "$arg" in
    --purge-data) PURGE_DATA=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请以 root 或 sudo 运行。" >&2
  exit 1
fi

echo "==> 停用并停止服务与定时器"
systemctl disable --now raricy-funds.service 2>/dev/null || true
systemctl disable --now raricy-funds-backup.timer 2>/dev/null || true
systemctl stop raricy-funds-backup.service 2>/dev/null || true

echo "==> 移除 systemd unit"
rm -f "$UNIT_DIR/raricy-funds.service" \
      "$UNIT_DIR/raricy-funds-backup.service" \
      "$UNIT_DIR/raricy-funds-backup.timer" \
      "$UNIT_DIR/raricy-funds-tunnel.service"
systemctl daemon-reload

echo "==> 移除代码目录（保留数据与凭据）"
rm -rf "$APP_DIR"

if [[ "$PURGE_DATA" -eq 1 ]]; then
  echo "==> 删除数据目录 $DATA_DIR（不可恢复）"
  rm -rf "$DATA_DIR"
else
  echo "    数据目录保留：$DATA_DIR"
fi
echo "    凭据文件保留：$CONF_DIR/funds.env（请自行决定是否清除）"

echo "==> 系统用户保留（$APP_USER）；如需删除：userdel $APP_USER"
