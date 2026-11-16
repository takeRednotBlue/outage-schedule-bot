# Mykolaiv outage schedule bot

Python Telegram bot for **Миколаївобленерго**, with a selectable outage queue per user (default **5.1**). Messages are in Ukrainian. It sends 15/30-minute reminders, scheduled start/end messages, schedule changes, and one warning per stale-data episode. `OFF`, `PROBABLY_OFF`, and `SURE_OFF` remain distinct. These are schedule/operator-status notifications, not confirmation of electricity at a home. No address or electricity account credentials are collected.

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

Send `/start` or `/menu` to open the inline interface. View today's/tomorrow's schedule, the next outage, settings, and bot status by tapping buttons. Navigation edits the same message. Alerts also include menu buttons. A few emojis mark important notifications; the controls use short Ukrainian labels.

To switch queues, tap **«Черга … · змінити»**, then the queue number. Subqueues appear side by side (1.1 / 1.2, etc.); a checkmark shows the current selection. Tap **«Показати графік»** to view its schedule. Settings offer 15/30-minute reminder buttons, pause/resume, queue selection, and subscription deletion. Deletion through the inline menu requires confirmation. Use **«Головне меню»** to return at any time.

Commands remain available: `/menu`, `/today`, `/tomorrow`, `/next`, `/queue`, `/settings 15`, `/settings 30`, `/status`, `/pause`, `/resume`, `/delete`. The explicit `/delete` command still deletes immediately. Resuming preserves the selected queue and reminder lead time. `/next` includes an ongoing interval. Only the Mykolaiv provider is supported.

Send `/queue` to open the queue buttons, or `/queue 2.1` (for example) to select one directly. Each user has one independent selection: `/today`, `/tomorrow`, `/next`, `/status`, reminders, and schedule-change notices all use it. Changing queues preserves reminder timing and pause state, clears pending change notices for the previous queue, and skips reminders whose due time predates the selection. Existing subscriptions migrate automatically to queue 5.1 without losing their settings. Queue names and IDs are refreshed from the operator's catalog; before the first successful fetch, `/queue` offers the known 1.1–6.2 choices. A missing queue never silently falls back to another queue's schedule. Older buttons are checked against the current catalog; deleted subscriptions are not recreated by navigation or queue-selection buttons.

Run **one instance per token/database**. Long polling requires no public port or domain. If this bot previously used webhooks, remove its webhook before starting long polling; the application does not change webhook settings automatically.

## Data and behavior

The following public endpoints were checked during development:

- `https://off.energy.mk.ua/api/v2/schedule/active`
- `https://off.energy.mk.ua/api/schedule/time-series`
- `https://off.energy.mk.ua/api/outage-queue/by-type/3`

The parser joins queue/time-slot IDs and derives the local calendar date from the schedule's timezone-aware `from`. Adjacent slots of the same category are merged. End-at-midnight slots roll to the next day. Missing publication differs from a published schedule with no intervals. Unknown categories, missing slot definitions, overlaps, or invalid queue mappings reject the refresh and retain the previous snapshot. Valid queue ID changes are followed using the operator's current catalog.

Supported categories match the [operator's public schedule legend](https://off.energy.mk.ua/): `OFF` is a scheduled outage, `PROBABLY_OFF` is a possible outage, and `SURE_OFF` is an operator-reported current outage ("Актуальне відключення"). The updated November 15–16 sample includes `SURE_OFF` alongside the other categories. It is preserved in storage, schedule-change detection, schedule commands, and reminders; it does not establish that a particular home has lost power.

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

For automatic updates after the first deployment, follow [GitHub Actions setup](deploy/AUTOMATION.md). Pushes to `main` will test and deploy a fresh release over SSH.

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

Keep application files owned by the deploy administrator, readable/executable by the service user. systemd creates `/var/lib/outage-bot` for the writable database. The application prefers systemd's `STATE_DIRECTORY` over `DATABASE_PATH`, so an old relative database path in the environment file cannot redirect writes into the protected application directory. Outside systemd, `DATABASE_PATH` remains configurable. No inbound application port is needed. Use `/status` to check successful fetches after deployment. Pin the exact tested dependency versions for repeatable production deployments.

Back up SQLite using its online backup API (copying just the database while WAL is active is unsafe):

```bash
sudo -u outagebot /opt/outage-bot/.venv/bin/python -c 'import sqlite3; src=sqlite3.connect("/var/lib/outage-bot/bot.sqlite3"); dst=sqlite3.connect("/var/lib/outage-bot/backup.sqlite3"); src.backup(dst); dst.close(); src.close()'
```

Copy the resulting backup to separate storage. Retain backups according to your privacy needs; `/delete` removes live subscription/delivery records, not old backups. Periodically check service status, disk usage, provider schema changes, and hosting eligibility.
