from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import AsyncMock, patch

import httpx

from outage_bot.app import Application, database_path
from outage_bot.schedule import KYIV, Interval, Schedule
from outage_bot.storage import Store


class DatabaseConfigurationTests(unittest.TestCase):
    def test_service_uses_writable_state_directory_despite_old_env_path(self):
        with tempfile.TemporaryDirectory() as state:
            with patch.dict("os.environ", {"STATE_DIRECTORY": state,
                                          "DATABASE_PATH": "/proc/outage-bot/data/bot.sqlite3"}):
                store = Store(database_path())
                store.subscribe(123, 0)
                store.db.close()
            self.assertTrue((Path(state) / "bot.sqlite3").is_file())

    def test_local_database_path_remains_configurable(self):
        with patch.dict("os.environ", {"DATABASE_PATH": "custom/bot.sqlite3"}, clear=True):
            self.assertEqual(database_path(), "custom/bot.sqlite3")


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime.now(UTC)
        self.store = Store(":memory:")
        self.bot = AsyncMock()
        self.app = Application(self.bot, None, self.store)
        self.store.subscribe(123, 0)
        self.schedule = Schedule([Interval(self.now - timedelta(seconds=10),
                                           self.now + timedelta(hours=1), "OFF")],
                                 {self.now.astimezone(KYIV).date()})
        self.store.put("schedule", self.schedule.to_json())
        self.store.put("last_success", self.now.timestamp())

    async def asyncTearDown(self):
        self.store.db.close()

    async def test_delivery_is_not_repeated(self):
        await self.app.notify()
        await self.app.notify()
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_failed_delivery_is_retried(self):
        self.bot.send_message.side_effect = RuntimeError("transient")
        await self.app.notify()
        self.bot.send_message.side_effect = None
        await self.app.notify()
        self.assertEqual(self.bot.send_message.await_count, 2)
        await self.app.notify()
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_stale_schedule_suppresses_events_and_warns_once(self):
        self.store.put("last_success", self.now.timestamp() - 3600)
        self.app.started -= 3600
        await self.app.notify()
        await self.app.notify()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn("призупинено", self.bot.send_message.call_args.args[1])

    async def test_pause_prevents_all_notifications(self):
        self.store.pause(123)
        await self.app.notify()
        self.bot.send_message.assert_not_awaited()

    async def test_pending_change_survives_failed_send(self):
        self.store.put("change:123", "revision")
        self.bot.send_message.side_effect = RuntimeError("transient")
        await self.app.notify()
        self.assertEqual(self.store.get("change:123"), "revision")
        self.bot.send_message.side_effect = None
        await self.app.notify()
        self.assertIsNone(self.store.get("change:123"))

    async def test_fetch_checks_queue_and_replaces_schedule(self):
        day = self.now.astimezone(KYIV).replace(hour=0, minute=0, second=0, microsecond=0)
        active = [{"from": day.isoformat(), "series": []}]
        slots = json.loads((Path(__file__).resolve().parents[1] / "time_series.json").read_text())
        def respond(request):
            data = active if request.url.path.endswith("active") else slots if request.url.path.endswith("time-series") else [{"id": 24, "name": "5.1", "deleted": 0}]
            return httpx.Response(200, json=data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            self.app.client = client
            await self.app.fetch()
        self.assertEqual(self.store.load_schedule().intervals, [])
        self.assertIsNotNone(self.store.get("change:123"))
        self.assertTrue(self.app.fresh(datetime.now(UTC).timestamp()))

    async def test_old_publication_does_not_refresh_freshness(self):
        before = self.store.get("last_success")
        def respond(request):
            if request.url.path.endswith("active"):
                value = [{"from": "2020-01-01T00:00:00Z", "series": []}]
            elif request.url.path.endswith("time-series"):
                value = []
            else:
                value = [{"id": 24, "name": "5.1"}]
            return httpx.Response(200, json=value)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            self.app.client = client
            with self.assertRaises(ValueError):
                await self.app.fetch()
        self.assertEqual(self.store.get("last_success"), before)
        self.assertEqual(self.store.load_schedule(), self.schedule)
