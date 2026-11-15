# Mykolaiv outage schedule bot

Python Telegram bot for **Миколаївобленерго, queue 5.1 (ID 24)**. Messages are in Ukrainian. It sends 15/30-minute reminders, scheduled start/end messages, schedule changes, and one warning per stale-data episode. `OFF` and `PROBABLY_OFF` remain distinct. These are schedule notifications, not confirmation of electricity at a home. No address or electricity account credentials are collected.

## Run locally

Requires Python 3.12+ and internet access to Telegram and `off.energy.mk.ua`.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
chmod 600 .env
# Edit .env: set BOT_TOKEN from Telegram @BotFather.
set -a
source .env
set +a
.venv/bin/python -m outage_bot.app
```

The application reads environment variables; it does not automatically load `.env`. Never commit your real token. Start a private conversation with your bot and send `/start`. Telegram bots cannot initiate a conversation with you.

Commands: `/today`, `/tomorrow`, `/next`, `/settings 15`, `/settings 30`, `/status`, `/pause`, `/resume`, `/delete`. Resuming preserves the selected reminder lead time. `/next` includes an ongoing interval. Only this provider/queue is supported in v0.1.

Run **one instance per token/database**. Long polling requires no public port or domain. If this bot previously used webhooks, remove its webhook before starting long polling; the application does not change webhook settings automatically.

## Data and behavior

The following public endpoints were checked during development:

- `https://off.energy.mk.ua/api/v2/schedule/active`
- `https://off.energy.mk.ua/api/schedule/time-series`
- `https://off.energy.mk.ua/api/outage-queue/by-type/3`

The parser joins queue/time-slot IDs and derives the local calendar date from the schedule's timezone-aware `from`. Adjacent slots of the same category are merged. End-at-midnight slots roll to the next day. Missing publication differs from a published schedule with no intervals. Unknown categories, missing slot definitions, overlaps, or changed queue mapping reject the refresh and retain the previous snapshot.

Each polling cycle fetches once for all users. Defaults: fetch every 180 seconds, check notifications every 20 seconds, suppress timed alerts after 900 seconds without a valid refresh. Fetch failures use bounded exponential backoff. A successful fetch must include today's publication; tomorrow-only or old publications are not considered fresh. Freshness means successful retrieval, not a guarantee that the operator has updated its underlying data.

SQLite persists subscriptions, the latest schedule, pending schedule-change notices, and delivery IDs. Changes replace obsolete intervals; reminders are derived from the current snapshot. Only events due in the last five minutes are caught up, and none predating subscription/resume are sent. Changes arriving less than the configured lead time before an outage produce a schedule-change notice; the bot does not invent a missed advance reminder. Possible intervals also trigger explicitly labelled reminders. A category change at an interval boundary does not trigger a restoration message.

Failed sends are retried while still relevant; Telegram rate-limit responses are respected. Blocked bots pause the subscription. A crash between Telegram accepting a message and the delivery record being committed can cause a duplicate. Telegram/network outages and sudden operator changes can delay or prevent advance warning. A changed interval can produce a new start/end message. End messages describe the end of the published interval, never confirmed restoration.

The root JSON files are development samples from November 2026, **not fallback live data**. Keep them for parser tests. The half-hour local slot model cannot unambiguously represent repeated/skipped times on DST transition days; timed notifications on those days are suppressed until a provider-specific interpretation is verified.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover saved API responses, interval merging, midnight rollover, timezone conversion, possible outages, malformed responses, stale-data suppression, changes, failed deliveries, reminder timing and restart persistence. Live Telegram delivery requires your own bot token and a private-chat subscription.

## Free deployment options

1. **Oracle Always Free VM:** recommended conventional Python option if an eligible instance is available. Uses persistent SQLite and a systemd process. Capacity and free-resource policies apply; monitor current eligibility. [Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm), [FAQ](https://www.oracle.com/cloud/free/faq/).
2. **Existing Linux machine:** same setup, no additional hosting subscription. Needs uninterrupted power and internet, which can be difficult during outages.
3. **Cloudflare Python Workers + D1:** alternative architecture, requiring a webhook/cron entry point and D1 storage adapter. This repository's long-running process is not directly deployable there. Validate free CPU limits before choosing it. [Python cron support](https://developers.cloudflare.com/changelog/post/2025-04-22-python-worker-cron-triggers/), [limits](https://developers.cloudflare.com/workers/platform/limits/).

GitHub Actions scheduling can be delayed/dropped, so use it for CI rather than time-sensitive reminders. Render Free sleeps and has ephemeral local storage. New PythonAnywhere free accounts lack scheduled tasks. See [GitHub](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows), [Render](https://render.com/docs/free), [PythonAnywhere](https://help.pythonanywhere.com/pages/FreeAccountsFeatures/). Free hosting does not guarantee delivery timing.

## Deploy to a Linux VM

On an Ubuntu/Debian VM, install Python 3.12+ with venv support. Copy the project to `/opt/outage-bot`, excluding `.env`, `.venv`, local databases and caches. Build its virtual environment there and install with `pip install .` (no editable install needed in deployment).

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin outagebot
sudo install -m 600 .env.example /etc/outage-bot.env
# Edit /etc/outage-bot.env and set BOT_TOKEN.
sudo install -m 644 deploy/outage-bot.service /etc/systemd/system/outage-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now outage-bot
sudo systemctl status outage-bot
sudo journalctl -u outage-bot -n 50
```

Keep application files owned by the deploy administrator, readable/executable by the service user. systemd creates `/var/lib/outage-bot` for the writable database; that path overrides `.env`'s local path. No inbound application port is needed. Use `/status` to check successful fetches after deployment. Pin the exact tested dependency versions for repeatable production deployments.

Back up SQLite using its online backup API (copying just the database while WAL is active is unsafe):

```bash
sudo -u outagebot /opt/outage-bot/.venv/bin/python -c 'import sqlite3; src=sqlite3.connect("/var/lib/outage-bot/bot.sqlite3"); dst=sqlite3.connect("/var/lib/outage-bot/backup.sqlite3"); src.backup(dst); dst.close(); src.close()'
```

Copy the resulting backup to separate storage. Retain backups according to your privacy needs; `/delete` removes live subscription/delivery records, not old backups. Periodically check service status, disk usage, provider schema changes, and hosting eligibility.
