from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from outage_bot.engine import due_events
from outage_bot.schedule import Interval, Schedule, parse_schedule, render_day
from outage_bot.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.slots = json.loads((ROOT / "time_series.json").read_text())

    def schedule(self, rows, day="2026-11-13T22:00:00Z"):
        return parse_schedule([{"from": day, "series": [
            {"outage_queue_id": queue, "time_series_id": slot, "type": kind}
            for queue, slot, kind in rows]}], self.slots)

    def test_saved_provider_response(self):
        result = parse_schedule(json.loads((ROOT / "active.json").read_text()), self.slots)
        self.assertEqual(result.days, {date(2026, 11, 13)})
        self.assertTrue(result.intervals)
        self.assertEqual(Schedule.from_json(result.to_json()), result)

    def test_merge_midnight_and_queue_filter(self):
        result = self.schedule([(24, 47, "OFF"), (24, 48, "OFF"), (25, 1, "OFF")])
        self.assertEqual(result.intervals, [Interval(datetime(2026, 11, 14, 21, tzinfo=UTC),
                                                    datetime(2026, 11, 14, 22, tzinfo=UTC), "OFF")])

    def test_possible_and_definite_are_separate(self):
        result = self.schedule([(24, 1, "PROBABLY_OFF"), (24, 2, "OFF")])
        self.assertEqual(len(result.intervals), 2)
        self.assertIn("Можливе", render_day(result, date(2026, 11, 14)))

    def test_unknown_status_or_slot_fails_closed(self):
        for rows in [[(24, 1, "NEW_STATUS")], [(24, 99, "OFF")]]:
            with self.assertRaises((ValueError, KeyError)):
                self.schedule(rows)

    def test_no_publication_differs_from_no_intervals(self):
        result = self.schedule([])
        self.assertIn("ще не опубліковано", render_day(result, date(2026, 11, 15)))
        self.assertIn("немає інтервалів", render_day(result, date(2026, 11, 14)))

    def test_summer_timezone(self):
        result = self.schedule([(24, 1, "OFF")], "2026-07-01T21:00:00Z")
        self.assertEqual(result.intervals[0].start, datetime(2026, 7, 1, 21, tzinfo=UTC))


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 14, 10, tzinfo=UTC)
        self.interval = Interval(self.start, self.start + timedelta(hours=1), "OFF")
        self.schedule = Schedule([self.interval], {self.start.date()})

    def test_reminder_start_end_and_obsolete(self):
        for delta, expected in [(-30, "before"), (0, "start"), (60, "end")]:
            events = list(due_events(self.schedule, self.start + timedelta(minutes=delta), 30, 0))
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0][0].endswith(expected))
        self.assertEqual(list(due_events(self.schedule, self.start + timedelta(hours=2), 30, 0)), [])

    def test_no_backlog_for_new_subscription(self):
        self.assertEqual(list(due_events(self.schedule, self.start + timedelta(seconds=60), 30,
                                        (self.start + timedelta(seconds=30)).timestamp())), [])

    def test_category_boundary_is_not_restoration(self):
        self.schedule.intervals.append(Interval(self.interval.end, self.interval.end + timedelta(hours=1), "PROBABLY_OFF"))
        events = list(due_events(self.schedule, self.interval.end, 30, 0))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0][0].endswith("start"))

    def test_dst_transition_suppresses_timed_events(self):
        start = datetime(2026, 10, 25, 1, tzinfo=UTC)
        schedule = Schedule([Interval(start, start + timedelta(hours=1), "OFF")], {start.date()})
        self.assertEqual(list(due_events(schedule, start, 30, 0)), [])

    def test_storage_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "state.sqlite3")
            store = Store(path)
            store.subscribe(1, 0)
            store.reminder(1, 15)
            store.put("schedule", self.schedule.to_json())
            store.mark(1, "event", self.start.timestamp())
            store.db.close()
            restarted = Store(path)
            self.assertTrue(restarted.delivered(1, "event"))
            self.assertEqual(restarted.subscriber(1)["minutes"], 15)
            self.assertEqual(restarted.load_schedule(), self.schedule)
            restarted.pause(1)
            self.assertEqual(restarted.subscribers(), [])
            restarted.forget(1)
            self.assertIsNone(restarted.subscriber(1))
            self.assertFalse(restarted.delivered(1, "event"))
            restarted.db.close()


if __name__ == "__main__":
    unittest.main()
