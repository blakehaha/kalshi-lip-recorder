#!/usr/bin/env bash
# Install / repair the pmbot watcher in its OWN checkout (/opt/pmbot), separate
# from /opt/kalshi-lip-recorder, which other services (lip-recorder, lip-watch)
# run from on whatever branch they need. Never touches that folder. Safe to re-run.
#
#   git clone -b claude/hopeful-einstein-tbivhk https://github.com/blakehaha/kalshi-lip-recorder.git /opt/pmbot
#   bash /opt/pmbot/deploy/setup-pmbot.sh you@example.com     # contact for the SEC User-Agent
set -euxo pipefail

REPO=/opt/pmbot
OUT=/var/lib/lip/pmbot
cd "$REPO"
id lip >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin lip
chown -R lip:lip "$REPO"
[ -x .venv/bin/python ] || sudo -u lip python3 -m venv .venv
sudo -u lip .venv/bin/pip install -q -r requirements.txt

mkdir -p /etc/lip "$OUT"
if [ ! -f /etc/lip/pmbot.env ]; then
  [ -n "${1:-}" ] || { echo "usage: bash $0 you@example.com" >&2; exit 2; }
  echo "PMBOT_UA=\"pmbot $1\"" > /etc/lip/pmbot.env
fi
chmod 600 /etc/lip/pmbot.env
# Earlier versions wrote to /var/lib/lip/watch, which the lip-watch recorder also uses.
# Move only our own files (signals.jsonl, prices-*.jsonl); its parquet stays put.
shopt -s nullglob
for f in /var/lib/lip/watch/signals.jsonl /var/lib/lip/watch/prices-*.jsonl; do
  mv -n "$f" "$OUT/"
done
chown -R lip:lip "$OUT"

set -a; . /etc/lip/pmbot.env; set +a
sudo -u lip --preserve-env=PMBOT_UA .venv/bin/python -m pmbot.watch --once --out /var/lib/lip/pmbot-test
rm -rf /var/lib/lip/pmbot-test

cp deploy/pmbot-watch.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable pmbot-watch
systemctl restart pmbot-watch
sleep 8
systemctl status pmbot-watch --no-pager -l | head -12 || true
systemctl is-active lip-recorder lip-watch || true    # the other project's services, untouched
