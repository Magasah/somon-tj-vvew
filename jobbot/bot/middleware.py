"""Кто может пользоваться ботом (v1.2): владелец и добавленные им друзья.

Срабатывает до фильтров и любого хендлера и кладёт в данные хендлера `role`
("owner" | "friend") и для друга — `friend` (User).

Незнакомец: его первое сообщение превращается в заявку — владельцу приходит сообщение с
кнопками «Добавить / Отклонить», самому человеку — один короткий ответ. Повторные сообщения
и нажатия кнопок незнакомцев игнорируются молча (только запись в лог).
"""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Filter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from jobbot.storage import Storage, normalize_username

log = logging.getLogger("bot.access")

REQUEST_SENT_TEXT = (
    "⏳ Это личный бот вакансий. Заявка на доступ отправлена его владельцу — "
    "когда он вас добавит, придёт сообщение."
)


def event_chat_id(event: TelegramObject) -> int | None:
    """chat_id, из которого пришло событие (сообщение или нажатие инлайн-кнопки)."""
    if isinstance(event, Message):
        return event.chat.id
    if isinstance(event, CallbackQuery):
        return event.message.chat.id if event.message else event.from_user.id
    return None


def _is_private(event: TelegramObject) -> bool:
    if isinstance(event, Message):
        return event.chat.type == "private"
    if isinstance(event, CallbackQuery) and event.message is not None:
        return event.message.chat.type == "private"
    return False


class Role(Filter):
    """Фильтр хендлеров по роли из AccessMiddleware: `Role("owner")`, `Role("friend")`."""

    def __init__(self, *roles: str) -> None:
        self.roles = roles

    async def __call__(self, event: TelegramObject, role: str | None = None) -> bool:
        return role in self.roles


class AccessMiddleware(BaseMiddleware):
    def __init__(self, owner_chat_id: int, storage: Storage) -> None:
        self._owner_chat_id = owner_chat_id
        self._storage = storage

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat_id = event_chat_id(event)
        if chat_id == self._owner_chat_id:
            data["role"] = "owner"
            return await handler(event, data)
        if chat_id is None or not _is_private(event):
            log.warning("игнорирую событие не из личного чата: chat_id=%s", chat_id)
            return None

        sender = getattr(event, "from_user", None)
        username = normalize_username(sender.username) if sender else None
        name = sender.full_name if sender else None
        bot: Bot | None = data.get("bot")

        friend = await self._storage.get_user(chat_id)
        if friend is None and await self._storage.has_invite(username):
            # Приглашён по @username — теперь знаем его chat_id.
            await self._storage.add_user(chat_id, username=username, name=name)
            friend = await self._storage.get_user(chat_id)
            log.info("приглашённый @%s открыл бота (chat_id=%d)", username, chat_id)
            await self._notify_owner(
                bot,
                f"✅ @{html.escape(username or '')} открыл бота и добавлен по приглашению. "
                "Настройте ему фильтры: /admin",
            )
        if friend is not None:
            if (friend.username, friend.name) != (username, name):
                await self._storage.update_user_identity(chat_id, username, name)
            if friend.status == "blocked":
                # Написал боту — значит, разблокировал. Возобновляем отправку.
                await self._storage.set_user_status(chat_id, "active")
            data["role"] = "friend"
            data["friend"] = await self._storage.get_user(chat_id)
            return await handler(event, data)

        log.warning("игнорирую событие из чужого чата: chat_id=%s", chat_id)
        if not isinstance(event, Message):
            return None
        is_new = await self._storage.record_request(chat_id, username, name)
        if is_new:
            await self._notify_new_request(bot, chat_id, username, name)
            try:
                await event.answer(REQUEST_SENT_TEXT)
            except TelegramAPIError as exc:
                log.warning("не удалось ответить незнакомцу: %s", exc)
        return None

    async def _notify_new_request(
        self, bot: Bot | None, chat_id: int, username: str | None, name: str | None
    ) -> None:
        from jobbot.bot.admin import AdminCB

        who = html.escape(name or "Без имени")
        if username:
            who += f" (@{html.escape(username)})"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Добавить", callback_data=AdminCB(a="rqa", u=chat_id).pack()
                    ),
                    InlineKeyboardButton(
                        text="🚫 Отклонить", callback_data=AdminCB(a="rqd", u=chat_id).pack()
                    ),
                ]
            ]
        )
        await self._notify_owner(
            bot,
            f"🙋 <b>Заявка на доступ</b>\n{who}\nID: <code>{chat_id}</code>",
            keyboard,
        )

    async def _notify_owner(
        self, bot: Bot | None, text: str, keyboard: InlineKeyboardMarkup | None = None
    ) -> None:
        if bot is None:
            return
        try:
            await bot.send_message(
                self._owner_chat_id, text, parse_mode="HTML", reply_markup=keyboard
            )
        except TelegramAPIError as exc:
            log.warning("не удалось уведомить владельца: %s", exc)
