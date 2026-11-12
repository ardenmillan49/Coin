#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"
repo=$(pwd -P)
service_user=$(id -un)
case "$repo" in *[!a-zA-Z0-9_./-]*) echo "Use a simple absolute project path"; exit 1;; esac
if [ ! -x .venv/bin/python ] || [ ! -f "${COIN_CONFIG_DIR:-$HOME/.config/coin}/telegram.json" ]; then
    echo "Run bash start.sh and finish Telegram pairing first."
    exit 1
fi
unit=$(mktemp)
trap 'rm -f "$unit"' EXIT
cat >"$unit" <<EOF
[Unit]
Description=Coin Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=$service_user
WorkingDirectory=$repo
ExecStart=$repo/.venv/bin/python -u $repo/bot.py
EnvironmentFile=-/etc/coin-bot.env
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 644 "$unit" /etc/systemd/system/coin-bot.service
sudo systemctl daemon-reload
sudo systemctl enable coin-bot
sudo systemctl restart coin-bot
sudo systemctl is-active coin-bot
