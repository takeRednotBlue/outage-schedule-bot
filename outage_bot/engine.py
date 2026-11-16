"""Pure event selection: persisted delivery records handle retry and restart."""
from datetime import datetime, timedelta

from .schedule import KYIV, Schedule, describe, transition_day


def due_events(schedule: Schedule, now: datetime, minutes: int, since: float):
    if transition_day(now.astimezone(KYIV).date()):
        return
    # Catch up only recent events; never deliver yesterday's reminders after downtime.
    for interval in schedule.intervals:
        start_text = ("⚡ Оператор позначив інтервал як актуальне відключення."
                      if interval.kind == "SURE_OFF" else "⚡ Початок інтервалу за графіком")
        events = [
            ("before", interval.start - timedelta(minutes=minutes),
             f"⏰ Нагадування за {minutes} хв\nЧерга 5.1 · час за Києвом\n\n{describe(interval)}"),
            ("start", interval.start,
             f"{start_text}\nЧерга 5.1 · час за Києвом\n\n{describe(interval)}\n\nФактичне знеструмлення за вашою адресою не підтверджено."),
            ("end", interval.end,
             f"Інтервал за графіком завершився\nЧерга 5.1 · час за Києвом\n\n{describe(interval)}\n\nФактичне відновлення не підтверджено."),
        ]
        for kind, at, text in events:
            if at.timestamp() < since or not timedelta(0) <= now - at <= timedelta(minutes=5):
                continue
            if kind == "before" and now >= interval.start:
                continue
            # A boundary between adjacent categories is not restoration.
            if kind == "end" and any(i.start == interval.end for i in schedule.intervals):
                continue
            yield f"{interval.key}:{kind}", text
