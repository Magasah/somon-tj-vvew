"""Доступ к боту только у владельца: чужие сообщения молча игнорируются."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

log = logging.getLogger("bot.access")


def event_chat_id(event: TelegramObject) -> int | None:
    """chat_id, из которого пришло событие (сообщение или нажатие инлайн-кнопки)."""
    if isinstance(event, Message):
        return event.chat.id
    if isinstance(event, CallbackQuery):
        return event.message.chat.id if event.message else event.from_user.id
    return None


class OwnerOnlyMiddleware(BaseMiddleware):
    """Пропускает событие дальше, только если оно из чата `OWNER_CHAT_ID`.

    Ставится как outer-middleware, то есть срабатывает до фильтров и любого хендлера.
    Чужим ничего не отвечаем (чтобы не подтверждать, что бот существует) — только лог.
    """

    def __init__(self, owner_chat_id: int) -> None:
        self._owner_chat_id = owner_chat_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat_id = event_chat_id(event)
        if chat_id != self._owner_chat_id:
            log.warning("игнорирую событие из чужого чата: chat_id=%s", chat_id)
            return None
        return await handler(event, data)
