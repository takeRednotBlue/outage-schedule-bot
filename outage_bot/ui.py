"""Small inline menus shared by commands, callbacks, and alerts."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def button(text, action):
    return InlineKeyboardButton(text=text, callback_data=f"ui:{action}")


def menu(subscriber):
    queue = subscriber["queue"] if subscriber else "5.1"
    rows = [
        [button(f"🔢 Черга {queue} · змінити", "queue")],
        [button("📅 Сьогодні", "today"), button("🗓️ Завтра", "tomorrow")],
        [button("⚡ Наступне відключення", "next")],
        [button("⚙️ Налаштування", "settings"), button("ℹ️ Стан бота", "status")],
    ]
    if subscriber:
        rows.append([button("⏸️ Призупинити" if subscriber["active"] else "🔔 Увімкнути сповіщення",
                            "pause" if subscriber["active"] else "resume")])
    else:
        rows.append([button("🔔 Увімкнути сповіщення", "start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def queue_menu(available, selected):
    rows = []
    # Keep the two subqueues of each group side by side.
    for name in available:
        group = name.split(".")[0]
        if not rows or rows[-1][0].callback_data.split(":")[-1].split(".")[0] != group or len(rows[-1]) >= 2:
            rows.append([])
        rows[-1].append(button(f"✓ {name}" if name == selected else name, f"queue:{name}"))
    rows.extend([[button("📋 Показати графік", "today")], [button("🏠 Головне меню", "menu")]])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_menu(subscriber):
    if not subscriber:
        return menu(None)
    minutes = subscriber["minutes"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [button(f"{'✓ ' if minutes == n else ''}{n} хв", f"settings:{n}") for n in (15, 30)],
        [button("🔢 Змінити чергу", "queue")],
        [button("⏸️ Призупинити" if subscriber["active"] else "🔔 Увімкнути сповіщення",
                "pause" if subscriber["active"] else "resume")],
        [button("🗑️ Видалити підписку", "delete")],
        [button("🏠 Головне меню", "menu")],
    ])


def delete_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Так, видалити", "delete_confirm"), button("Скасувати", "settings")],
    ])
