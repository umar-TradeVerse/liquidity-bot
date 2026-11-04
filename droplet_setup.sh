#!/usr/bin/env bash
# =====================================================================
#  TradeVerse liquidity bot — one-time setup on a DigitalOcean Droplet
#  Run in the Droplet's Web Console (as root):
#    bash <(curl -fsSL https://raw.githubusercontent.com/umar-TradeVerse/liquidity-bot/main/scripts/droplet_setup.sh)
#
#  What it does (safe to re-run; it skips steps already done):
#    1. Installs Python, git, firewall, automatic security updates
#    2. Forces IPv4 for outgoing traffic (CoinDCX key is bound to an IPv4)
#    3. Verifies the outgoing IP is exactly the one bound on CoinDCX
#    4. Installs the bot under its own locked-down user
#    5. Asks for your secrets HERE (hidden) — stored only on this server
#    6. Runs the test suite; refuses to start if anything fails
#    7. Starts the bot with auto-restart, and auto-updates from GitHub
#       every 5 minutes (an update is deployed ONLY if its tests pass)
#    8. Adds helper commands: botlogs, botlogs-send, botstatus
# =====================================================================
set -euo pipefail

BOT=liquidity-bot
REPO=https://github.com/umar-TradeVerse/liquidity-bot.git
BRANCH=main
EXPECTED_IP=206.189.138.66
BASE=/opt/bots/$BOT
ENVF=/etc/bots/$BOT.env

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m!! %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" = 0 ] || die "Run this as root (the Web Console logs you in as root)."

say "1/8  Installing system packages (1-2 minutes)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git curl ufw unattended-upgrades >/dev/null
git config --system --add safe.directory '*'

say "2/8  Forcing IPv4 for outgoing connections"
grep -q '^precedence ::ffff:0:0/96  100' /etc/gai.conf 2>/dev/null \
  || echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf

say "3/8  Checking this server's outgoing IP"
OUT_IP=$(curl -4 -fsS --max-time 15 https://api.ipify.org || echo unknown)
echo "     Outgoing IPv4: $OUT_IP   (CoinDCX key must be bound to: $EXPECTED_IP)"
[ "$OUT_IP" = "$EXPECTED_IP" ] || die "Outgoing IP does not match. Stop here and send this screen to Claude."

say "4/8  Firewall (allow SSH only) + automatic security updates"
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

say "5/8  Installing the bot"
id bot >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin bot
mkdir -p /opt/bots /etc/bots
[ -d "$BASE/.git" ] || git clone -q --branch "$BRANCH" "$REPO" "$BASE"
mkdir -p "$BASE/data"
[ -x "$BASE/venv/bin/python" ] || python3 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install -q --upgrade pip
"$BASE/venv/bin/pip" install -q -r "$BASE/requirements.txt"
chown -R bot:bot "$BASE"

say "6/8  Your settings (secrets are hidden as you paste, and never leave this server)"
if [ -f "$ENVF" ]; then
  echo "     Settings already exist in $ENVF — keeping them."
else
  read -rsp "     Paste CoinDCX API KEY, then Enter: " K; echo
  read -rsp "     Paste CoinDCX API SECRET, then Enter: " S; echo
  read -rsp "     Paste Telegram BOT TOKEN, then Enter: " T; echo
  read -rp  "     Telegram CHAT ID: " C
  read -rp  "     Trade size in USDT [50]: " Z; Z=${Z:-50}
  [ -n "$K" ] && [ -n "$S" ] && [ -n "$T" ] && [ -n "$C" ] || die "A value was empty — re-run the script."
  umask 077
  cat > "$ENVF" <<EOF
COINDCX_API_KEY=$K
COINDCX_API_SECRET=$S
TELEGRAM_BOT_TOKEN=$T
TELEGRAM_CHAT_ID=$C
TRADE_SIZE_USD=$Z
PERSIST_DIR=$BASE/data
POOL_EQUAL_MODE=off
POOL_SESSION_MODE=off
BREAKOUT_MODE=off
DEBUG=false
EOF
  chmod 600 "$ENVF"
  unset K S T
  echo "     Saved to $ENVF (readable by root only)."
fi

say "7/8  Running the test suite before starting"
( cd "$BASE" && sudo -u bot "$BASE/venv/bin/python" tests/run_tests.py | tail -3 ) \
  || die "Tests failed — the bot was NOT started. Send this screen to Claude."

say "8/8  Starting the bot, auto-update and helper commands"
cat > /etc/systemd/system/$BOT.service <<EOF
[Unit]
Description=TradeVerse liquidity bot
After=network-online.target
Wants=network-online.target

[Service]
User=bot
WorkingDirectory=$BASE
EnvironmentFile=$ENVF
ExecStart=$BASE/venv/bin/python main.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

# --- auto-update: deploy new GitHub code ONLY if its tests pass ---------
cat > /usr/local/bin/bot-update <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
BOT=$1; BASE=/opt/bots/$BOT; STAGE=/opt/bots/.stage-$BOT; FAILED=/opt/bots/.failed-$BOT
cd "$BASE" || exit 0
git fetch -q origin main || { logger -t bot-update "$BOT: fetch failed (network?)"; exit 0; }
NEW=$(git rev-parse origin/main); [ "$(git rev-parse HEAD)" = "$NEW" ] && exit 0
[ -f "$FAILED" ] && [ "$(cat "$FAILED")" = "$NEW" ] && exit 0          # already rejected
rm -rf "$STAGE"; git clone -q --branch main "$(git remote get-url origin)" "$STAGE" || exit 0
"$BASE/venv/bin/pip" install -q -r "$STAGE/requirements.txt"
if (cd "$STAGE" && "$BASE/venv/bin/python" tests/run_tests.py >/tmp/$BOT-update-tests.txt 2>&1); then
  git reset -q --hard "$NEW"; chown -R bot:bot "$BASE"; rm -f "$FAILED"
  systemctl restart "$BOT"
  logger -t bot-update "$BOT: deployed ${NEW:0:7} (tests passed)"
else
  echo "$NEW" > "$FAILED"
  logger -t bot-update "$BOT: update ${NEW:0:7} NOT deployed — tests failed (see /tmp/$BOT-update-tests.txt)"
fi
rm -rf "$STAGE"
EOF
chmod 755 /usr/local/bin/bot-update

cat > /etc/systemd/system/bot-update@.service <<'EOF'
[Unit]
Description=Auto-update %i from GitHub (tests must pass)
[Service]
Type=oneshot
ExecStart=/usr/local/bin/bot-update %i
EOF
cat > /etc/systemd/system/bot-update@.timer <<'EOF'
[Unit]
Description=Check GitHub for %i updates every 5 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF

# --- helper commands ------------------------------------------------------
cat > /usr/local/bin/botlogs <<'EOF'
#!/usr/bin/env bash
# botlogs [bot] [since]   e.g.  botlogs liquidity-bot "2 hours ago"
journalctl -u "${1:-liquidity-bot}" -u "bot-update@${1:-liquidity-bot}" --since "${2:-1 hour ago}" --no-pager -o cat
EOF
cat > /usr/local/bin/botlogs-send <<'EOF'
#!/usr/bin/env bash
# botlogs-send [bot] [since]  -> sends the log file to your Telegram
BOT=${1:-liquidity-bot}; SINCE=${2:-24 hours ago}; ENVF=/etc/bots/$BOT.env
T=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENVF" | cut -d= -f2-); C=$(grep '^TELEGRAM_CHAT_ID=' "$ENVF" | cut -d= -f2-)
F=/tmp/${BOT}_$(date -u +%Y-%m-%d_%H%M).log
journalctl -u "$BOT" --since "$SINCE" --no-pager -o cat > "$F"
curl -s -F chat_id="$C" -F caption="$BOT logs since $SINCE" -F document=@"$F" \
  "https://api.telegram.org/bot$T/sendDocument" >/dev/null && echo "Sent $F to Telegram" || echo "Send failed"
EOF
cat > /usr/local/bin/botstatus <<'EOF'
#!/usr/bin/env bash
BOT=${1:-liquidity-bot}
systemctl --no-pager --lines=0 status "$BOT" | head -4
echo "Code version: $(git -C /opt/bots/$BOT log -1 --format='%h %s (%cr)')"
echo "Outgoing IP:  $(curl -4 -fsS --max-time 10 https://api.ipify.org)"
EOF
chmod 755 /usr/local/bin/botlogs /usr/local/bin/botlogs-send /usr/local/bin/botstatus

systemctl daemon-reload
systemctl enable --now "$BOT" >/dev/null
systemctl enable --now "bot-update@$BOT.timer" >/dev/null
sleep 20

say "DONE — the bot is running. First log lines:"
journalctl -u "$BOT" --no-pager -o cat -n 15
cat <<EOF

Useful commands (type in this console):
  botstatus                 running? code version, outgoing IP
  botlogs                   last hour of logs on screen
  botlogs-send              send the last 24h of logs to your Telegram
  systemctl restart $BOT    restart the bot
To change a setting:  nano $ENVF   then   systemctl restart $BOT
EOF
