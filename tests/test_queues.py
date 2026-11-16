from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from outage_bot.app import Application
from outage_bot.schedule import Interval, Schedule
from outage_bot.storage import Store


class QueueMigrationTests(unittest.TestCase):
    def test_old_subscription_migrates_and_selection_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "bot.sqlite3")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE subscribers (chat_id INTEGER PRIMARY KEY, minutes INTEGER NOT NULL, "
                       "active INTEGER NOT NULL, since REAL NOT NULL)")
            db.execute("INSERT INTO subscribers VALUES (1, 15, 0, 100)")
            db.commit()
            db.close()
            store = Store(path)
            self.assertEqual(store.subscriber(1)["queue"], "5.1")
            store.select_queue(1, "2.1", 200)
            store.db.close()
            store = Store(path)
            row = store.subscriber(1)
            self.assertEqual((row["queue"], row["minutes"], row["active"], row["since"]), ("2.1", 15, 0, 200))
            store.db.close()


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime(2026, 11, 16, 10, tzinfo=UTC)
        self.clock = patch("outage_bot.app.datetime", wraps=datetime)
        self.clock.start().now.return_value = self.now
        self.store = Store(":memory:")
        self.bot = AsyncMock()
        self.app = Application(self.bot, None, self.store)
        self.store.subscribe(1, 0)
        self.store.subscribe(2, 0)
        self.store.select_queue(2, "2.1", 0)
        self.store.put("queues", {"5.1": 24, "2.1": 16})
        self.store.put("last_success", self.now.timestamp())

    async def asyncTearDown(self):
        self.clock.stop()
        self.store.db.close()

    async def command(self, chat, text):
        message = SimpleNamespace(chat=SimpleNamespace(id=chat, type="private"), text=text, answer=AsyncMock())
        await self.app.command(message)
        return message.answer.call_args.args[0]

    def schedules(self):
        self.store.put("schedules", {
            name: Schedule([Interval(self.now - timedelta(seconds=10), self.now + timedelta(hours=1), kind)],
                           {self.now.date()}).to_json()
            for name, kind in [("5.1", "OFF"), ("2.1", "SURE_OFF")]})

    async def test_selection_is_private_and_preserves_settings(self):
        self.store.reminder(1, 15)
        self.store.pause(1)
        self.store.put("change:1", "old-queue-revision")
        text = await self.command(1, "/queue 2.1")
        self.assertIn("Обрано чергу 2.1", text)
        self.assertIn("призупиненими", text)
        row = self.store.subscriber(1)
        self.assertEqual((row["queue"], row["minutes"], row["active"]), ("2.1", 15, 0))
        self.assertEqual(row["since"], self.now.timestamp())
        self.assertIsNone(self.store.get("change:1"))
        await self.command(1, "/resume")
        self.assertEqual(self.store.subscriber(1)["queue"], "2.1")
        self.assertEqual(self.store.subscriber(1)["minutes"], 15)

    async def test_queue_list_and_invalid_input(self):
        _, keyboard = self.app.screen(1, "/queue")
        self.assertIn("ui:queue:2.1", [b.callback_data for row in keyboard.inline_keyboard for b in row])
        for value in ["99.1", "2.1 extra"]:
            self.assertIn("Невідома черга", await self.command(1, "/queue " + value))
        self.assertEqual(self.store.subscriber(1)["queue"], "5.1")
        self.assertIn("Спочатку", await self.command(99, "/queue 2.1"))
        self.assertIsNone(self.store.subscriber(99))

    async def test_display_and_alerts_follow_each_users_queue(self):
        self.schedules()
        for command in ["/today", "/next", "/status"]:
            text = await self.command(2, command)
            self.assertIn("2.1", text)
            self.assertNotIn("5.1", text)
        await self.app.notify()
        self.assertEqual(self.bot.send_message.await_count, 2)
        messages = {call.args[0]: call.args[1] for call in self.bot.send_message.call_args_list}
        self.assertIn("Черга 5.1", messages[1])
        self.assertNotIn("актуальне", messages[1])
        self.assertIn("Черга 2.1", messages[2])
        self.assertIn("актуальне", messages[2])

    async def test_identical_intervals_in_other_queue_are_not_deduplicated(self):
        self.schedules()
        snapshots = self.store.get("schedules")
        snapshots["2.1"] = snapshots["5.1"]
        self.store.put("schedules", snapshots)
        await self.app.notify()
        # Switch user 1 to a queue with the exact same interval after its start.
        self.store.select_queue(1, "2.1", 0)
        await self.app.notify()
        self.assertEqual([c.args[0] for c in self.bot.send_message.call_args_list], [1, 2, 1])

    async def test_fetch_uses_api_queue_ids_and_notifies_only_changed_queue(self):
        slots = json.loads((Path(__file__).resolve().parents[1] / "time_series.json").read_text())
        rows = [{"time_series_id": 40, "outage_queue_id": queue, "type": "OFF"} for queue in (240, 160)]
        def respond(request):
            if request.url.path.endswith("active"):
                value = [{"from": "2026-11-15T22:00:00Z", "series": rows}]
            elif request.url.path.endswith("time-series"):
                value = slots
            else:
                value = [{"id": 240, "name": "5.1"}, {"id": 160, "name": "2.1"}]
            return httpx.Response(200, json=value)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            self.app.client = client
            await self.app.fetch()
            self.assertEqual(self.store.load_schedule("2.1").intervals[0].kind, "OFF")
            rows[1]["type"] = "SURE_OFF"
            await self.app.fetch()
            self.assertIsNone(self.store.get("change:1"))
            self.assertIsNotNone(self.store.get("change:2"))
            # A failed refresh must leave all queue snapshots and freshness intact.
            before = self.store.get("schedules")
            success = self.store.get("last_success")
            rows[1]["type"] = "UNKNOWN"
            with self.assertRaises(ValueError):
                await self.app.fetch()
            self.assertEqual(self.store.get("schedules"), before)
            self.assertEqual(self.store.get("last_success"), success)

    async def test_missing_selected_queue_never_falls_back_to_default(self):
        self.store.put("schedule", Schedule([], {self.now.date()}).to_json())
        self.assertIsNone(self.store.load_schedule("2.1"))
        self.assertIn("недоступний", await self.command(2, "/today"))
