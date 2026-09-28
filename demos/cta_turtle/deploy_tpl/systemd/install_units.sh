#!/usr/bin/env bash
# 安装 systemd unit 文件 (render @占位符@ → 实际路径)
# 用法: sudo ./install_units.sh /home/ubuntu/projects/cta_turtle
#       sudo ./install_units.sh /home/ubuntu/projects/cta_turtle <MON_TOKEN> [TQ_USER TQ_PASS]
set -euo pipefail
PREFIX="${1:?usage: install_units.sh <prefix> [mon_token] [tq_user tq_pass]}"
MON_TOKEN="${2:-changeme}"
TQ_USER="${3:-}"
TQ_PASS="${4:-}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in "$DIR"/*.service "$DIR"/*.timer; do
    name="$(basename "$f")"
    sed -e "s|@PREFIX@|${PREFIX}|g" \
        -e "s|@MON_TOKEN@|${MON_TOKEN}|g" \
        -e "s|@TQ_USER@|${TQ_USER}|g" \
        -e "s|@TQ_PASS@|${TQ_PASS}|g" "$f" > "/etc/systemd/system/$name"
    echo "installed $name"
done
systemctl daemon-reload
systemctl enable wt-turtle-monitor.service wt-turtle-contracts.timer \
    wt-turtle-hots.timer wt-turtle-hotdaily.timer wt-turtle-premarket.timer
echo "enabled. 启动: systemctl start wt-turtle-monitor.service"
echo "注意: 重启 monitor 会级联重启 datakit+交易进程 (同 cgroup), 避免盘中操作"
