#!/usr/bin/env bash
# One-shot installer for yts-auto-sync on Dell (Debian 13).
# Run as user fmon. SUDO_PASS env var enables non-interactive sudo.
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$PWD"

_sudo() {
    if [ -n "${SUDO_PASS:-}" ]; then
        echo "$SUDO_PASS" | sudo -S -p '' "$@"
    else
        sudo "$@"
    fi
}

echo "── 1. 数据目录 (/mnt/extdata) ──────────────────────────────"
_sudo mkdir -p /mnt/extdata/{movies,torrents/incomplete,qbit/config}
_sudo chown -R 1000:1000 /mnt/extdata/{movies,torrents,qbit}

echo "── 2. .env ─────────────────────────────────────────────────"
if [ ! -f .env ]; then cp .env.example .env; echo "已创建 .env (默认值，可后续编辑)"; fi

echo "── 3. Python venv + 依赖 ───────────────────────────────────"
python3 -m venv .venv
.venv/bin/pip install -U pip wheel
.venv/bin/pip install -r requirements.txt

echo "── 4. qBittorrent (Docker) ────────────────────────────────"
if ! docker ps --format '{{.Names}}' | grep -q '^qbittorrent$'; then
    _sudo docker compose -f deploy/docker-compose.qbit.yml up -d
    echo "等待 qBit 启动…"
    for i in $(seq 1 20); do
        sleep 1
        curl -sf -m 2 http://127.0.0.1:8080 >/dev/null && break || true
    done
    # 提取 qBit 一次性密码（首次启动写在 docker logs）
    PASS=$(_sudo docker logs qbittorrent 2>&1 | grep -oE 'temporary password is provided.*: \S+' | tail -1 | awk '{print $NF}')
    if [ -n "$PASS" ]; then
        echo "默认 qBit 临时密码: $PASS"
        sed -i.bak "s|^QBIT_PASS=.*|QBIT_PASS=$PASS|" .env
        echo "已写入 .env (QBIT_PASS)"
    fi
fi

echo "── 5. systemd 服务 ────────────────────────────────────────"
_sudo cp deploy/yts-auto-sync.service /etc/systemd/system/
_sudo systemctl daemon-reload
_sudo systemctl enable --now yts-auto-sync
sleep 2
_sudo systemctl --no-pager status yts-auto-sync | head -10

echo
echo "── ✅ 完成 ─────────────────────────────────────────────────"
echo "Web UI:    http://10.10.10.10:4003"
echo "qBit UI:   http://10.10.10.10:8080  (admin / 见 .env QBIT_PASS)"
echo "日志:       journalctl -u yts-auto-sync -f"
echo "下载目录:   /mnt/extdata/movies"
