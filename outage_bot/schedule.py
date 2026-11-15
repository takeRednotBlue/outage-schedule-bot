"""Provider-independent intervals and Mykolaiv response parsing."""
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
SOURCE = "https://off.energy.mk.ua/"


def transition_day(day: date) -> bool:
    return (datetime.combine(day, time.min, KYIV).utcoffset() !=
            datetime.combine(day + timedelta(days=1), time.min, KYIV).utcoffset())


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Provider timestamp has no timezone")
    return result.astimezone(UTC)


@dataclass(frozen=True, order=True)
class Interval:
    start: datetime
    end: datetime
    kind: str

    @property
    def key(self) -> str:
        return f"{self.start.isoformat()}:{self.end.isoformat()}:{self.kind}"


@dataclass
class Schedule:
    intervals: list[Interval]
    days: set[date]

    def to_json(self) -> dict:
        return {
            "intervals": [[i.start.isoformat(), i.end.isoformat(), i.kind] for i in self.intervals],
            "days": sorted(d.isoformat() for d in self.days),
        }

    @classmethod
    def from_json(cls, value: dict) -> "Schedule":
        return cls([Interval(timestamp(a), timestamp(b), k) for a, b, k in value["intervals"]],
                   {date.fromisoformat(d) for d in value["days"]})


def parse_schedule(active: list, slots: list, queue_id: int = 24) -> Schedule:
    if not isinstance(active, list) or not isinstance(slots, list):
        raise ValueError("Expected provider arrays")
    slot_map = {s["id"]: (time.fromisoformat(s["start"]), time.fromisoformat(s["end"])) for s in slots}
    intervals = set()
    days = set()
    for schedule in active:
        day = timestamp(schedule["from"]).astimezone(KYIV).date()
        days.add(day)
        for row in schedule["series"]:
            if row["outage_queue_id"] != queue_id:
                continue
            kind = row["type"]
            if kind not in {"OFF", "PROBABLY_OFF"}:
                raise ValueError(f"Unknown outage type: {kind}")
            start_time, end_time = slot_map[row["time_series_id"]]
            start = datetime.combine(day, start_time, KYIV).astimezone(UTC)
            end_day = day + timedelta(days=1) if end_time <= start_time else day
            end = datetime.combine(end_day, end_time, KYIV).astimezone(UTC)
            if end <= start:
                raise ValueError("Invalid interval")
            intervals.add(Interval(start, end, kind))
    merged: list[Interval] = []
    for interval in sorted(intervals):
        if merged and merged[-1].kind == interval.kind and merged[-1].end == interval.start:
            previous = merged.pop()
            merged.append(Interval(previous.start, interval.end, interval.kind))
        else:
            if merged and interval.start < merged[-1].end:
                raise ValueError("Overlapping outage slots")
            merged.append(interval)
    return Schedule(merged, days)


def describe(interval: Interval) -> str:
    label = "Відключення за графіком" if interval.kind == "OFF" else "Можливе відключення"
    return (f"{interval.start.astimezone(KYIV):%d.%m %H:%M} — "
            f"{interval.end.astimezone(KYIV):%d.%m %H:%M}: {label}")


def render_day(schedule: Schedule, day: date) -> str:
    if day not in schedule.days:
        return f"На {day:%d.%m} графік ще не опубліковано або недоступний."
    start = datetime.combine(day, time.min, KYIV).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, KYIV).astimezone(UTC)
    rows = [describe(i) for i in schedule.intervals if i.start < end and i.end > start]
    return f"Черга 5.1 · {day:%d.%m}\n" + ("\n".join(rows) if rows else "У публікації немає інтервалів відключення для цієї черги.")
