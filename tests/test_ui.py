from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import Chat, Message

from outage_bot.app import Application
from outage_bot.storage import Store


def buttons(keyboard):
    return {b.callback_data: b.text for row in keyboard.inline_keyboard for b in row}


class InlineMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.store.subscribe(1, 0)
        self.store.put("queues", {"1.1": 14, "1.2": 15, "2.1": 16, "5.1": 24})
        self.bot = AsyncMock()
        self.app = Application(self.bot, None, self.store)

    async def asyncTearDown(self):
        self.store.db.close()

    async def click(self, data, owner=1, chat_type="private"):
        query = SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=owner), answer=AsyncMock(),
            message=Message(message_id=42, date=datetime.now(UTC), chat=Chat(id=1, type=chat_type), text="Menu"),
        )
        await self.app.callback(query)
        query.answer.assert_awaited_once()
        return query

    def edited(self):
        return self.bot.edit_message_text.call_args

    async def test_command_opens_inline_queue_picker(self):
        message = SimpleNamespace(chat=SimpleNamespace(id=1, type="private"), text="/queue", answer=AsyncMock())
        await self.app.command(message)
        keyboard = message.answer.call_args.kwargs["reply_markup"]
        self.assertEqual([b.text for b in keyboard.inline_keyboard[0]], ["1.1", "1.2"])
        self.assertEqual(buttons(keyboard)["ui:queue:5.1"], "✓ 5.1")

    async def test_switch_queue_updates_picker_and_preserves_pause_and_timing(self):
        self.store.pause(1)
        self.store.reminder(1, 15)
        self.store.put("change:1", "old")
        await self.click("ui:queue:2.1")
        row = self.store.subscriber(1)
        self.assertEqual((row["queue"], row["active"], row["minutes"]), ("2.1", 0, 15))
        self.assertIsNone(self.store.get("change:1"))
        self.assertIn("Обрано чергу 2.1", self.edited().args[0])
        self.assertEqual(buttons(self.edited().kwargs["reply_markup"])["ui:queue:2.1"], "✓ 2.1")
        self.bot.send_message.assert_not_awaited()  # Navigation edits one menu message.

    async def test_stale_picker_cannot_select_removed_queue(self):
        self.store.put("queues", {"5.1": 24})
        await self.click("ui:queue:2.1")
        self.assertEqual(self.store.subscriber(1)["queue"], "5.1")
        self.assertIn("Невідома черга", self.edited().args[0])

    async def test_navigation_does_not_resume_or_reset_subscription(self):
        self.store.pause(1)
        for action in ["menu", "today", "tomorrow", "next", "status", "queue", "settings"]:
            await self.click("ui:" + action)
        row = self.store.subscriber(1)
        self.assertEqual((row["active"], row["since"]), (0, 0))
        self.assertIn("ui:resume", buttons(self.app.screen(1, "/menu")[1]))

    async def test_reminder_buttons_change_and_mark_current_value(self):
        await self.click("ui:settings:15")
        self.assertEqual(self.store.subscriber(1)["minutes"], 15)
        self.assertEqual(buttons(self.edited().kwargs["reply_markup"])["ui:settings:15"], "✓ 15 хв")
        await self.click("ui:settings:99")
        self.assertEqual(self.store.subscriber(1)["minutes"], 15)

    async def test_delete_button_requires_confirmation_and_cancel_preserves_data(self):
        await self.click("ui:delete")
        self.assertIsNotNone(self.store.subscriber(1))
        self.assertIn("ui:delete_confirm", buttons(self.edited().kwargs["reply_markup"]))
        await self.click("ui:settings")
        self.assertIsNotNone(self.store.subscriber(1))
        await self.click("ui:delete_confirm")
        self.assertIsNone(self.store.subscriber(1))
        # An old picker does not recreate the deleted subscription.
        await self.click("ui:queue:2.1")
        self.assertIsNone(self.store.subscriber(1))
        self.assertIn("ui:start", buttons(self.edited().kwargs["reply_markup"]))

    async def test_untrusted_callback_cannot_modify_subscription(self):
        for data, owner, chat_type in [("ui:queue:2.1", 2, "private"), ("ui:delete_confirm", 1, "group"),
                                       ("ui:delete_confirm:extra", 1, "private"), ("anything", 1, "private")]:
            query = await self.click(data, owner=owner, chat_type=chat_type)
            self.assertTrue(query.answer.call_args.kwargs["show_alert"])
        self.assertEqual(self.store.subscriber(1)["queue"], "5.1")
        self.bot.edit_message_text.assert_not_awaited()

    async def test_repeated_tap_ignores_only_not_modified_error(self):
        method = EditMessageText(chat_id=1, message_id=42, text="Menu")
        self.bot.edit_message_text.side_effect = TelegramBadRequest(method=method, message="Bad Request: message is not modified")
        await self.click("ui:menu")
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_text.side_effect = TelegramBadRequest(method=method, message="Bad Request: unexpected error")
        with self.assertRaises(TelegramBadRequest):
            await self.click("ui:menu")

    async def test_old_uneditable_menu_falls_back_to_new_message(self):
        self.bot.edit_message_text.side_effect = TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=42, text="Menu"), message="Bad Request: message can't be edited")
        await self.click("ui:menu")
        self.bot.send_message.assert_awaited_once()
        self.assertIn("ui:queue", buttons(self.bot.send_message.call_args.kwargs["reply_markup"]))
