import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import time

import httpx
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import BotCommand, CallbackQuery, LinkPreviewOptions, Message

from .engine import due_events
from .schedule import KYIV, SOURCE, describe, parse_schedule, render_day
from .storage import Store
from .ui import delete_menu, menu, queue_menu, settings_menu

LOG = logging.getLogger(__name__)
BASE = "https://off.energy.mk.ua"
DEFAULT_QUEUES = [f"{group}.{part}" for group in range(1, 7) for part in (1, 2)]
HELP_TEXT = (
    "/menu — головне меню з кнопками\n\n"
    "Графік\n"
    "/today — сьогодні\n/tomorrow — завтра\n/next — поточне або наступне відключення\n\n"
    "Налаштування\n"
    "/queue — обрати чергу\n"
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
        catalog = {}
        for row in queues:
            if row.get("deleted") or row.get("type_id", 3) != 3:
                continue
            name, queue_id = row["name"], row["id"]
            if not isinstance(name, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", name) or len(name) > 16:
                raise ValueError("Invalid queue name")
            if name in catalog or not isinstance(queue_id, int) or queue_id in catalog.values():
                raise ValueError("Invalid queue mapping")
            catalog[name] = queue_id
        if not catalog:
            raise ValueError("Empty queue catalog")
        schedules = {name: parse_schedule(active, slots, queue_id) for name, queue_id in catalog.items()}
        now = datetime.now(UTC)
        if any(now.astimezone(KYIV).date() not in schedule.days for schedule in schedules.values()):
            raise ValueError("Provider has no schedule for today")
        # Compare each queue independently, so unrelated changes don't notify users.
        def relevant(s):
            return ([i for i in s.intervals if i.end > now],
                    {d for d in s.days if d >= now.astimezone(KYIV).date()})
        updates = {}
        for name, schedule in schedules.items():
            previous = self.store.load_schedule(name)
            if previous is not None and relevant(previous) != relevant(schedule):
                content = json.dumps(schedule.to_json(), sort_keys=True)
                revision = hashlib.sha256(content.encode()).hexdigest()[:20]
                for subscriber in self.store.subscribers():
                    if subscriber["queue"] == name:
                        updates[f"change:{subscriber['chat_id']}"] = revision
        # Publish the complete snapshot only after every queue has validated.
        updates.update(schedules={name: s.to_json() for name, s in schedules.items()},
                       queues=catalog, last_success=now.timestamp(), last_error=None)
        self.store.put_many(updates)
        LOG.info("Schedule fetched for %s queues", len(schedules))

    async def send_once(self, chat_id, key, text, now):
        if self.store.delivered(chat_id, key):
            return
        await self.bot.send_message(chat_id, text + f"\n\nДжерело: {SOURCE}",
                                    reply_markup=menu(self.store.subscriber(chat_id)),
                                    link_preview_options=LinkPreviewOptions(is_disabled=True))
        self.store.mark(chat_id, key, now)
        await asyncio.sleep(0.05)

    async def notify(self):
        if time.time() < self.not_before:
            return
        now = datetime.now(UTC)
        for subscriber in self.store.subscribers():
            chat_id = subscriber["chat_id"]
            queue = subscriber["queue"]
            schedule = self.store.load_schedule(queue)
            try:
                if not self.fresh(now.timestamp()):
                    # One warning per failure episode, after the initial grace period.
                    if now.timestamp() - self.started >= self.stale_seconds:
                        await self.send_once(chat_id, f"stale:{self.store.get('last_success', 0)}",
                                             "⚠️ Не вдається оновити графік\n\nНагадування призупинено до отримання свіжих даних.", now.timestamp())
                    continue
                if schedule is None:
                    await self.send_once(chat_id, f"queue-unavailable:{queue}",
                                         f"⚠️ Графік черги {queue} недоступний.\nОберіть іншу чергу кнопкою нижче.", now.timestamp())
                    continue
                revision = self.store.get(f"change:{chat_id}")
                if revision:
                    day = now.astimezone(KYIV).date()
                    await self.send_once(chat_id, f"queue:{queue}:change:{revision}",
                                         "🔄 Графік змінився\nПопередні нагадування можуть бути неактуальними.\n\n" +
                                         render_day(schedule, day, queue) + "\n\n" + render_day(schedule, day + timedelta(days=1), queue), now.timestamp())
                    if self.store.get(f"change:{chat_id}") == revision:
                        self.store.put(f"change:{chat_id}", None)
                for key, text in due_events(schedule, now, subscriber["minutes"], subscriber["since"], queue):
                    current = self.store.subscriber(chat_id)
                    if current is None or not current["active"] or current["queue"] != queue:
                        break
                    if queue == "5.1" and self.store.delivered(chat_id, key):
                        continue  # Preserve default-queue delivery history from older versions.
                    await self.send_once(chat_id, f"queue:{queue}:{key}", text, now.timestamp())
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

    def screen(self, chat_id, command, arguments=()):
        words = [command, *arguments]
        now = datetime.now(UTC)
        subscriber = self.store.subscriber(chat_id)
        queue = subscriber["queue"] if subscriber else "5.1"
        schedule = self.store.load_schedule(queue)
        available = sorted(self.store.get("queues", dict.fromkeys(DEFAULT_QUEUES)),
                           key=lambda name: tuple(map(int, name.split("."))))
        if command in {"/start", "/resume"}:
            self.store.subscribe(chat_id, now.timestamp())
            self.store.put(f"change:{chat_id}", None)
            subscriber = self.store.subscriber(chat_id)
            text = (f"🔔 Сповіщення увімкнено\nМиколаївобленерго · черга {queue}\n"
                    f"Нагадування за {subscriber['minutes']} хв\n\n"
                    "Оберіть дію нижче. Щоб змінити чергу, натисніть кнопку з її номером.\n\n"
                    "Повідомлення базуються на графіку й не підтверджують фактичний стан світла.")
        elif command == "/queue":
            if len(words) == 1:
                text = (f"🔢 Поточна черга: {queue}\n\nНатисніть номер потрібної черги.\n"
                        "Вибір змінює графік і сповіщення лише для вас.")
            elif len(words) != 2 or words[1] not in available:
                text = "Невідома черга.\nОберіть доступну чергу нижче."
            elif subscriber is None:
                text = "Спочатку увімкніть сповіщення, потім оберіть чергу."
            elif words[1] == queue:
                text = f"Черга {queue} вже обрана.\nНатисніть «Показати графік»."
            else:
                self.store.select_queue(chat_id, words[1], now.timestamp())
                text = (f"✅ Обрано чергу {words[1]}\n\nГрафіки та сповіщення тепер для цієї черги.\n"
                        "Натисніть «Показати графік», щоб переглянути розклад.")
                if not subscriber["active"]:
                    text += "\nСповіщення залишаються призупиненими. Увімкнути їх можна в головному меню."
        elif command == "/delete":
            self.store.forget(chat_id)
            text = "Підписку та історію сповіщень видалено."
        elif command == "/pause":
            self.store.pause(chat_id)
            text = "⏸️ Сповіщення призупинено\n\nГрафік доступний. Щоб відновити нагадування, натисніть «Увімкнути сповіщення»."
        elif command == "/settings":
            subscriber = self.store.subscriber(chat_id)
            if subscriber is None:
                text = "Спочатку увімкніть сповіщення кнопкою нижче."
            elif len(words) == 2 and words[1] in {"15", "30"}:
                self.store.reminder(chat_id, int(words[1]))
                text = f"🔔 Нагадування за {words[1]} хв."
            else:
                text = (f"⚙️ Налаштування · черга {queue}\n\nНагадування за {subscriber['minutes']} хв\n"
                        f"Сповіщення: {'увімкнено' if subscriber['active'] else 'призупинено'}\n\n"
                        "Оберіть час нагадування нижче.")
        elif command == "/status":
            last = self.store.get("last_success")
            stamp = datetime.fromtimestamp(last, KYIV).strftime("%d.%m %H:%M:%S") if last else "ще немає"
            subscriber = self.store.subscriber(chat_id)
            indicator = "✅" if self.fresh(now.timestamp()) else "⚠️"
            text = (f"{indicator} Стан бота\nМиколаївобленерго · черга {queue}\n\n"
                    f"Дані: {'свіжі' if self.fresh(now.timestamp()) else 'застарілі / недоступні'}\n"
                    f"Останнє оновлення: {stamp} (Київ)\n\n"
                    f"Сповіщення: {'увімкнено' if subscriber and subscriber['active'] else 'вимкнено'}")
            if subscriber:
                text += f"\nНагадування за {subscriber['minutes']} хв"
            if schedule is None:
                text += "\nГрафік обраної черги недоступний."
        elif command in {"/today", "/tomorrow", "/next"}:
            if schedule is None:
                text = "Графік поки недоступний. Спробуйте пізніше."
            elif command == "/next":
                upcoming = next((i for i in schedule.intervals if i.end > now), None)
                if upcoming:
                    title = "Поточний інтервал" if upcoming.start <= now else "Наступний інтервал"
                    text = f"{title}\nЧерга {queue} · час за Києвом\n\n{describe(upcoming)}"
                else:
                    text = "У доступному графіку немає наступних інтервалів."
            else:
                day = now.astimezone(KYIV).date() + timedelta(days=command == "/tomorrow")
                text = render_day(schedule, day, queue)
            if schedule and not self.fresh(now.timestamp()):
                text = "⚠️ Збережені дані застарілі.\n\n" + text
        elif command == "/menu":
            text = (f"Миколаївобленерго · черга {queue}\n\nОберіть дію нижче.\n"
                    "Час у графіках — за Києвом.")
        else:
            text = HELP_TEXT
        current = self.store.subscriber(chat_id)
        if command == "/queue" and current:
            keyboard = queue_menu(available, current["queue"])
        elif command == "/settings":
            keyboard = settings_menu(current)
        else:
            keyboard = menu(current)
        return text + f"\n\nДжерело: {SOURCE}", keyboard

    async def command(self, message: Message):
        if message.chat.type != "private" or not message.text:
            return
        words = message.text.split()
        text, keyboard = self.screen(message.chat.id, words[0].split("@")[0], words[1:])
        await message.answer(text, reply_markup=keyboard,
                             link_preview_options=LinkPreviewOptions(is_disabled=True))

    async def callback(self, query: CallbackQuery):
        message = query.message
        if not isinstance(message, Message) or message.chat.type != "private" or message.chat.id != query.from_user.id:
            await query.answer("Відкрийте особистий чат із ботом.", show_alert=True)
            return
        parts = (query.data or "").split(":")
        actions = {"menu", "today", "tomorrow", "next", "queue", "settings", "status",
                   "pause", "resume", "start", "help", "delete", "delete_confirm"}
        if len(parts) not in (2, 3) or parts[0] != "ui" or parts[1] not in actions or (len(parts) == 3 and parts[1] not in {"queue", "settings"}):
            await query.answer("Ця кнопка недоступна. Відкрийте /menu.", show_alert=True)
            return
        # Acknowledge promptly so Telegram stops the loading indicator.
        await query.answer()
        action = parts[1]
        if action == "delete":
            text, keyboard = "Видалити підписку та історію сповіщень?\n\nНагадування буде вимкнено.", delete_menu()
        else:
            command = "/delete" if action == "delete_confirm" else f"/{action}"
            text, keyboard = self.screen(message.chat.id, command, parts[2:])
        try:
            await self.bot.edit_message_text(text, chat_id=message.chat.id, message_id=message.message_id,
                                             reply_markup=keyboard,
                                             link_preview_options=LinkPreviewOptions(is_disabled=True))
        except TelegramBadRequest as exc:
            if "message is not modified" in exc.message.lower():
                return
            if "message to edit not found" not in exc.message.lower() and "message can't be edited" not in exc.message.lower():
                raise
            await self.bot.send_message(message.chat.id, text, reply_markup=keyboard,
                                        link_preview_options=LinkPreviewOptions(is_disabled=True))


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
        dispatcher.message.register(app.command, Command("start", "resume", "delete", "pause", "settings", "status", "today", "tomorrow", "next", "help", "queue", "menu"))
        dispatcher.callback_query.register(app.callback)
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in [
            ("menu", "Головне меню"), ("today", "Графік на сьогодні"), ("tomorrow", "Графік на завтра"), ("next", "Наступне відключення"),
            ("queue", "Обрати чергу"), ("settings", "Налаштування"), ("status", "Свіжість даних"), ("pause", "Призупинити"), ("resume", "Відновити"), ("delete", "Видалити підписку")]])
        task = asyncio.create_task(app.worker())
        polling = asyncio.create_task(dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"]))
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
