#!/usr/bin/env bash
# Invoked as root over SSH. The SSH user must have passwordless sudo.
set -Eeuo pipefail

archive="${SUDO_USER:+/home/$SUDO_USER/}outage-bot-source.tar.gz"
if [[ -z ${SUDO_USER:-} || ! -f "$archive" ]]; then
    echo 'Run through sudo from an Ubuntu deployment user after uploading the archive.' >&2
    exit 1
fi
test -f /etc/outage-bot.env
id outagebot > /dev/null

base=/opt/outage-bot
mkdir -p "$base/releases"
release=$(mktemp -d "$base/releases/release-XXXXXXXX")
chmod 755 "$release"
tar -xzf "$archive" -C "$release" --no-same-owner
python3 -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip install "$release"
(cd / && "$release/.venv/bin/python" -c 'import outage_bot.app')

# The existing service retains its user, environment, and persistent database.
override_dir=/etc/systemd/system/outage-bot.service.d
override="$override_dir/20-release.conf"
mkdir -p "$override_dir"
backup=$(mktemp)
had_override=0
if [[ -f "$override" ]]; then
    cp "$override" "$backup"
    had_override=1
fi
previous=$(readlink "$base/current" || true)
switched=0
rollback() {
    result=$?
    trap - EXIT
    if (( result != 0 && switched )); then
        echo 'Deployment failed; restoring the previous service configuration.' >&2
        if (( had_override )); then
            cp "$backup" "$override"
        else
            rm -f "$override"
        fi
        if [[ -n "$previous" ]]; then
            ln -sfn "$previous" "$base/current"
        else
            rm -f "$base/current"
        fi
        systemctl daemon-reload
        systemctl restart outage-bot || true
    fi
    rm -f "$backup"
    exit "$result"
}
trap rollback EXIT

switched=1
systemctl stop outage-bot
ln -sfn "$release" "$base/current"
cat > "$override" <<'UNIT'
[Service]
WorkingDirectory=/opt/outage-bot/current
ExecStart=
ExecStart=/opt/outage-bot/current/.venv/bin/python -m outage_bot.app
UNIT
systemctl daemon-reload
systemctl restart outage-bot
# Detect an immediate startup crash/restart loop. This is a process check,
# not confirmation of Telegram delivery or provider freshness.
sleep 15
systemctl is-active --quiet outage-bot
test "$(systemctl show outage-bot --property=NRestarts --value)" = 0
switched=0
echo "Deployed $release"
echo 'Check /status in Telegram to confirm source freshness.'
