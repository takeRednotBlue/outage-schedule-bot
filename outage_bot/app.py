import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import time

import httpx
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

from .engine import due_events
from .schedule import KYIV, SOURCE, describe, parse_schedule, render_day
from .storage import Store

LOG = logging.getLogger(__name__)
BASE = "https://off.energy.mk.ua"
HELP_TEXT = (
    "Графік\n"
    "/today — сьогодні\n/tomorrow — завтра\n/next — поточне або наступне відключення\n\n"
    "Налаштування\n"
    "/settings — час нагадування\n/status — стан бота й свіжість даних\n"
    "/pause — призупинити сповіщення\n/resume — відновити\n/delete — видалити підписку"
)


def database_path():
    # systemd supplies a writable StateDirectory, even if an old env file
    # sets DATABASE_PATH to a relative path under the read-only application.
    state_directory = os.environ.get("STATE_DIRECTORY")
    if state_directory:
        return str(Path(state_directory.split(":")[0]) / "bot.sqlite3")
    return os.environ.get("DATABASE_PATH", "data/bot.sqlite3")


class Application:
    def __init__(self, bot, client, store, poll_seconds=180, stale_seconds=900):
        self.bot, self.client, self.store = bot, client, store
        self.poll_seconds, self.stale_seconds = poll_seconds, stale_seconds
        self.started = time.time()
        self.not_before = 0

    def fresh(self, now):
        return now - self.store.get("last_success", 0) < self.stale_seconds

    async def fetch(self):
        paths = ["/api/v2/schedule/active", "/api/schedule/time-series", "/api/outage-queue/by-type/3"]
        responses = await asyncio.gather(*(self.client.get(BASE + p) for p in paths))
        for response in responses:
            response.raise_for_status()
        active, slots, queues = [r.json() for r in responses]
        if not any(q["id"] == 24 and q["name"] == "5.1" and not q.get("deleted") for q in queues):
            raise ValueError("Queue 5.1 mapping changed")
        schedule = parse_schedule(active, slots)
        now = datetime.now(UTC)
        if now.astimezone(KYIV).date() not in schedule.days:
            raise ValueError("Provider has no schedule for today")
        previous = self.store.load_schedule()
        if previous is not None:
            # Ignore expired intervals when detecting changes; retain published dates.
            def relevant(s):
                return ([i for i in s.intervals if i.end > now],
                        {d for d in s.days if d >= now.astimezone(KYIV).date()})
            if relevant(previous) != relevant(schedule):
                content = json.dumps(schedule.to_json(), sort_keys=True)
                revision = hashlib.sha256(content.encode()).hexdigest()[:20]
                for subscriber in self.store.subscribers():
                    self.store.put(f"change:{subscriber['chat_id']}", revision)
        self.store.put("schedule", schedule.to_json())
        self.store.put("last_success", now.timestamp())
        self.store.put("last_error", None)
        LOG.info("Schedule fetched: %s intervals", len(schedule.intervals))

    async def send_once(self, chat_id, key, text, now):
        if self.store.delivered(chat_id, key):
            return
        await self.bot.send_message(chat_id, text + f"\n\nДжерело: {SOURCE}")
        self.store.mark(chat_id, key, now)
        await asyncio.sleep(0.05)

    async def notify(self):
        if time.time() < self.not_before:
            return
        now = datetime.now(UTC)
        schedule = self.store.load_schedule()
        for subscriber in self.store.subscribers():
            chat_id = subscriber["chat_id"]
            try:
                if not self.fresh(now.timestamp()):
                    # One warning per failure episode, after the initial grace period.
                    if now.timestamp() - self.started >= self.stale_seconds:
                        await self.send_once(chat_id, f"stale:{self.store.get('last_success', 0)}",
                                             "⚠️ Не вдається оновити графік\n\nНагадування призупинено до отримання свіжих даних.", now.timestamp())
                    continue
                if schedule is None:
                    continue
                revision = self.store.get(f"change:{chat_id}")
                if revision:
                    day = now.astimezone(KYIV).date()
                    await self.send_once(chat_id, f"change:{revision}",
                                         "🔄 Графік змінився\nПопередні нагадування можуть бути неактуальними.\n\n" +
                                         render_day(schedule, day) + "\n\n" + render_day(schedule, day + timedelta(days=1)), now.timestamp())
                    self.store.put(f"change:{chat_id}", None)
                for key, text in due_events(schedule, now, subscriber["minutes"], subscriber["since"]):
                    await self.send_once(chat_id, key, text, now.timestamp())
            except TelegramForbiddenError:
                self.store.pause(chat_id)
            except TelegramRetryAfter as exc:
                self.not_before = time.time() + exc.retry_after
                return  # Retry on a later cycle; delivery remains unmarked.
            except Exception as exc:
                # Avoid logging request URLs containing a bot token.
                LOG.warning("Notification failed (%s)", type(exc).__name__)
        self.store.prune(now.timestamp())

    async def worker(self):
        next_fetch = 0
        failures = 0
        while True:
            if time.monotonic() >= next_fetch:
                try:
                    await self.fetch()
                    failures = 0
                except Exception as exc:
                    failures += 1
                    self.store.put("last_error", type(exc).__name__)
                    LOG.warning("Provider fetch failed (%s)", type(exc).__name__)
                delay = min(self.poll_seconds, 15 * 2 ** min(failures, 6)) if failures else self.poll_seconds
                next_fetch = time.monotonic() + delay
            await self.notify()
            await asyncio.sleep(20)

    async def command(self, message: Message):
        if message.chat.type != "private" or not message.text:
            return
        words = message.text.split()
        command = words[0].split("@")[0]
        chat_id = message.chat.id
        now = datetime.now(UTC)
        schedule = self.store.load_schedule()
        if command in {"/start", "/resume"}:
            self.store.subscribe(chat_id, now.timestamp())
            self.store.put(f"change:{chat_id}", None)
            subscriber = self.store.subscriber(chat_id)
            text = ("🔔 Сповіщення увімкнено\nМиколаївобленерго · черга 5.1\n"
                    f"Нагадування за {subscriber['minutes']} хв\n\n"
                    f"{HELP_TEXT}\n\n"
                    "Повідомлення базуються на графіку й не підтверджують фактичний стан світла.")
        elif command == "/delete":
            self.store.forget(chat_id)
            text = "Підписку та історію сповіщень видалено."
        elif command == "/pause":
            self.store.pause(chat_id)
            text = "Сповіщення призупинено\n\n/resume — увімкнути знову."
        elif command == "/settings":
            subscriber = self.store.subscriber(chat_id)
            if subscriber is None:
                text = "Спочатку виконайте /start."
            elif len(words) == 2 and words[1] in {"15", "30"}:
                self.store.reminder(chat_id, int(words[1]))
                text = f"🔔 Нагадування за {words[1]} хв."
            else:
                text = (f"Нагадування за {subscriber['minutes']} хв\n\n"
                        "Оберіть час:\n/settings 15 — за 15 хв\n/settings 30 — за 30 хв")
        elif command == "/status":
            last = self.store.get("last_success")
            stamp = datetime.fromtimestamp(last, KYIV).strftime("%d.%m %H:%M:%S") if last else "ще немає"
            subscriber = self.store.subscriber(chat_id)
            indicator = "✅" if self.fresh(now.timestamp()) else "⚠️"
            text = (f"{indicator} Стан бота\nМиколаївобленерго · черга 5.1\n\n"
                    f"Дані: {'свіжі' if self.fresh(now.timestamp()) else 'застарілі / недоступні'}\n"
                    f"Останнє оновлення: {stamp} (Київ)\n\n"
                    f"Сповіщення: {'увімкнено' if subscriber and subscriber['active'] else 'вимкнено'}")
            if subscriber:
                text += f"\nНагадування за {subscriber['minutes']} хв"
        elif command in {"/today", "/tomorrow", "/next"}:
            if schedule is None:
                text = "Графік поки недоступний. Спробуйте пізніше."
            elif command == "/next":
                upcoming = next((i for i in schedule.intervals if i.end > now), None)
                if upcoming:
                    title = "Поточний інтервал" if upcoming.start <= now else "Наступний інтервал"
                    text = f"{title}\nЧерга 5.1 · час за Києвом\n\n{describe(upcoming)}"
                else:
                    text = "У доступному графіку немає наступних інтервалів."
            else:
                day = now.astimezone(KYIV).date() + timedelta(days=command == "/tomorrow")
                text = render_day(schedule, day)
            if schedule and not self.fresh(now.timestamp()):
                text = "⚠️ Збережені дані застарілі.\n\n" + text
        else:
            text = HELP_TEXT
        await message.answer(text + f"\n\nДжерело: {SOURCE}")


async def run():
    token = os.environ.get("BOT_TOKEN", "")
    if not token or token == "replace_with_BotFather_token":
        raise SystemExit("Set BOT_TOKEN in the environment (see .env.example).")
    poll = int(os.environ.get("POLL_SECONDS", "180"))
    stale = int(os.environ.get("STALE_SECONDS", "900"))
    if poll < 60 or stale < poll * 2:
        raise SystemExit("POLL_SECONDS must be >=60; STALE_SECONDS must be >=2*POLL_SECONDS")
    path = database_path()
    try:
        store = Store(path)
    except OSError as exc:
        raise SystemExit(f"Cannot create database directory for {path!r}. "
                         "Check the service StateDirectory and directory ownership.") from exc
    async with Bot(token) as bot, httpx.AsyncClient(timeout=20, headers={"User-Agent": "MykolaivScheduleBot/0.1"}) as client:
        app = Application(bot, client, store, poll, stale)
        dispatcher = Dispatcher()
        dispatcher.message.register(app.command, Command("start", "resume", "delete", "pause", "settings", "status", "today", "tomorrow", "next", "help"))
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in [
            ("today", "Графік на сьогодні"), ("tomorrow", "Графік на завтра"), ("next", "Наступне відключення"),
            ("settings", "Налаштування"), ("status", "Свіжість даних"), ("pause", "Призупинити"), ("resume", "Відновити"), ("delete", "Видалити підписку")]])
        task = asyncio.create_task(app.worker())
        polling = asyncio.create_task(dispatcher.start_polling(bot, allowed_updates=["message"]))
        try:
            done, _ = await asyncio.wait({task, polling}, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()  # Restart the service if the notification worker fails.
        finally:
            for running in (task, polling):
                running.cancel()
            await asyncio.gather(task, polling, return_exceptions=True)
            store.db.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
