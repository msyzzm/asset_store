#!/usr/bin/env bash
# One-time setup of the "更新" button on the Docker host (the LXC). Run from the repo,
# with the containers up:  sudo deploy/updater/install.sh
# Re-running it is safe (e.g. after moving the repo). Remove: sudo deploy/updater/install.sh --uninstall
set -euo pipefail

UNITS=(asset-updater.service asset-updater.path asset-updater-check.service asset-updater-check.timer)
LIB=/usr/local/lib/asset-updater
ENV_FILE=/etc/asset-updater.env

if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root：sudo $0" >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now asset-updater.path asset-updater-check.timer 2>/dev/null || true
    for unit in "${UNITS[@]}"; do rm -f "/etc/systemd/system/$unit"; done
    systemctl daemon-reload
    rm -rf "$LIB" "$ENV_FILE"
    echo "已卸载。"
    exit 0
fi

REPO_DIR=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO_DIR"
GIT_USER=$(stat -c %U "$REPO_DIR")

# The data volume as the host sees it: where the web container has /data mounted.
WEB=$(docker compose ps -q web 2>/dev/null || true)
if [ -z "$WEB" ]; then
    echo "web 容器没在运行：先 docker compose up -d" >&2
    exit 1
fi
DATA_DIR=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$WEB")
if [ -z "$DATA_DIR" ] || [ ! -d "$DATA_DIR" ]; then
    echo "找不到 web 容器的 /data 数据卷" >&2
    exit 1
fi

if ! runuser -u "$GIT_USER" -- git -C "$REPO_DIR" rev-parse --abbrev-ref '@{u}' > /dev/null 2>&1; then
    echo "当前分支没有上游分支（git pull 不知道从哪拉）：先 git branch --set-upstream-to=origin/main" >&2
    exit 1
fi

install -D -m 755 deploy/updater/asset-updater.sh "$LIB/asset-updater.sh"
cat > "$ENV_FILE" <<EOF
REPO_DIR=$REPO_DIR
DATA_DIR=$DATA_DIR
GIT_USER=$GIT_USER
EOF
# The containers now running were built from this checkout (as far as we can tell).
if [ ! -s "$DATA_DIR/update.deployed" ]; then
    runuser -u "$GIT_USER" -- git -C "$REPO_DIR" rev-parse --short HEAD > "$DATA_DIR/update.deployed"
fi

cat > /etc/systemd/system/asset-updater.service <<EOF
[Unit]
Description=Asset library: pull and rebuild when the web UI asks
After=docker.service

[Service]
Type=oneshot
EnvironmentFile=$ENV_FILE
ExecStart=$LIB/asset-updater.sh
TimeoutStartSec=45min
EOF

cat > /etc/systemd/system/asset-updater.path <<EOF
[Unit]
Description=Asset library: watch for update requests from the web UI

[Path]
PathExists=$DATA_DIR/update.request
Unit=asset-updater.service

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/asset-updater-check.service <<EOF
[Unit]
Description=Asset library: look for new versions

[Service]
Type=oneshot
EnvironmentFile=$ENV_FILE
ExecStart=$LIB/asset-updater.sh --check
TimeoutStartSec=10min
EOF

cat > /etc/systemd/system/asset-updater-check.timer <<EOF
[Unit]
Description=Asset library: look for new versions every hour

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable asset-updater.path asset-updater-check.timer
systemctl restart asset-updater.path asset-updater-check.timer  # re-runs pick up a new data dir
# Fills in the version shown on the page now; a failed first check shows there, it isn't fatal.
systemctl start asset-updater-check.service || echo "第一次检查更新失败，看网页或 journalctl -u asset-updater-check" >&2

echo "装好了。仓库 $REPO_DIR（git 用户 $GIT_USER），数据卷 $DATA_DIR。"
echo "网页任务栏里会出现「更新」一行；之后更新都在网页上点。"
