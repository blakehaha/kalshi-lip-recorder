# Handoff: get the LIP watch running on the droplet

Written for Claude Code running on Blake's PC, 2026-10-30.

## Context (read first)

- This repo is `blakehaha/kalshi-lip-recorder`, branch **`fv-engine`**. Never touch `main`.
- On a DigitalOcean droplet, an existing systemd service, `lip-recorder`, records Kalshi Liquidity Incentive Program (LIP) order books. Its unit file (`deploy/lip-recorder.service`) points to `/opt/kalshi-lip-recorder`, a venv at `.venv`, data in `/var/lib/lip`, and user `lip`. **Leave that service running and unchanged.**
- The goal is to run a **second, filtered recorder** for 3 days. It watches only the diesel and shipping-lane markets, per the pre-registered plan in `DIESEL_LIP_TEST.md`. On Sunday night, `analysis/watch_check.py` grades it PASS or FAIL.
- Everything in this repo is **read-only** against Kalshi (GET requests only, no order code). Keep it that way.

## Guardrails

- Don't add any code that places orders, and don't add API keys or secrets to the repo.
- Make no geolocation or VPN changes of any kind.
- Ask Blake before anything destructive or hard to undo: deleting files, reinstalling Python, changing firewall or SSH config, or touching the existing `lip-recorder` service.
- If SSH access fails, stop and tell Blake exactly what's missing. Don't guess credentials.

## Steps

1. **Reach the droplet.** Check `~/.ssh/config` and `~/.ssh/` for a key or host entry. If `doctl` is installed, run `doctl compute droplet list` for the IP. If you can't find the IP or key, ask Blake for the IP, or tell him to use DigitalOcean's web console (Droplets, then Access, then Launch Droplet Console) and run the commands below there.

2. **Find the repo and update it.**
   ```bash
   systemctl cat lip-recorder | grep -E 'WorkingDirectory|ExecStart|User'
   cd /opt/kalshi-lip-recorder            # use the WorkingDirectory printed above
   ls -ld . && stat -c %U .git            # note who owns the repo
   sudo -u <owner> git fetch origin && sudo -u <owner> git checkout fv-engine && sudo -u <owner> git pull
   .venv/bin/python -m pip install -q pandas pyarrow pytest   # only if missing
   .venv/bin/python -m pytest -q          # expect all tests to pass
   ```

3. **Install the watch as its own systemd service** so it survives reboots. Copy the existing unit and change only the name, description and `ExecStart`:
   ```bash
   sudo cp /etc/systemd/system/lip-recorder.service /etc/systemd/system/lip-watch.service
   sudo sed -i 's/^Description=.*/Description=Kalshi LIP watch (diesel + shipping lanes)/' /etc/systemd/system/lip-watch.service
   sudo sed -i 's#^ExecStart=.*#ExecStart=/opt/kalshi-lip-recorder/.venv/bin/python -m lip.recorder --out /var/lib/lip/watch --prefix KXDIESEL KXHORMUZ KXBABELMANDEB KXSUEZ --interval 600#' /etc/systemd/system/lip-watch.service
   sudo systemctl daemon-reload && sudo systemctl enable --now lip-watch
   ```
   If the existing unit lives somewhere other than `/etc/systemd/system/`, use the path `systemctl cat` printed. Adjust paths if the WorkingDirectory differs.

4. **Verify.** Wait about 2 minutes for the first sweep, then:
   ```bash
   systemctl is-active lip-watch lip-recorder         # both should print "active"
   journalctl -u lip-watch -n 20 --no-pager           # expect "sweep: N/N markets -> /var/lib/lip/watch/..."
   ls /var/lib/lip/watch/*/ | head
   .venv/bin/python analysis/watch_check.py /var/lib/lip/watch   # should run without errors (verdict is meaningless after 1 sweep)
   ```

5. **Stop there.** Don't place orders or start the live test. That happens only after the Sunday check passes, and Blake does it by hand.

## Report back to Blake (short)

- The droplet IP and how you got in.
- Repo path, branch and commit (`git log --oneline -1`).
- Test result (`N passed`).
- `systemctl is-active` output for both services.
- The last 3 lines of `journalctl -u lip-watch`.
- The Sunday command, verbatim:
  `cd /opt/kalshi-lip-recorder && .venv/bin/python analysis/watch_check.py /var/lib/lip/watch`

## Optional, only if Blake asks: election-night spread log

If an `arbscan.py` (Kalshi vs Polymarket scanner from September) exists on the PC, run it **read-only** with `--loop 300` through Nov 4. Write its output to a log file. Place no trades. If you can't find it, skip this.
