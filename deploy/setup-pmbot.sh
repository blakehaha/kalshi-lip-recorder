#!/usr/bin/env bash
# Install / repair the pmbot watcher on a droplet that already has this repo at
# /opt/kalshi-lip-recorder. Safe to re-run.
#
#   bash deploy/setup-pmbot.sh you@example.com     # contact for the SEC User-Agent
set -euxo pipefail

REPO=/opt/kalshi-lip-recorder
cd "$REPO"
id lip >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin lip
chown -R lip:lip "$REPO"
[ -x .venv/bin/python ] || sudo -u lip python3 -m venv .venv
sudo -u lip .venv/bin/pip install -q -r requirements.txt

mkdir -p /etc/lip /var/lib/lip/watch
if [ ! -f /etc/lip/pmbot.env ]; then
  [ -n "${1:-}" ] || { echo "usage: bash $0 you@example.com" >&2; exit 2; }
  echo "PMBOT_UA=\"pmbot $1\"" > /etc/lip/pmbot.env
fi
chmod 600 /etc/lip/pmbot.env
chown -R lip:lip /var/lib/lip

set -a; . /etc/lip/pmbot.env; set +a
sudo -u lip --preserve-env=PMBOT_UA .venv/bin/python -m pmbot.watch --once --out /var/lib/lip/watch-test
rm -rf /var/lib/lip/watch-test

cp deploy/pmbot-watch.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable pmbot-watch
systemctl restart pmbot-watch
sleep 8
systemctl status pmbot-watch --no-pager -l
journalctl -u pmbot-watch -n 30 --no-pager
