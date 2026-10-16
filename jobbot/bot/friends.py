"""Команды для друзей (v1.2): что видит человек, которому владелец открыл бота."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from jobbot.bot.filters_menu import (
    FILTERS_BUTTON,
    filters_reply_keyboard,
    matching_for,
    profile_summary,
)
from jobbot.bot.middleware import Role
from jobbot.storage import User, from_iso

if TYPE_CHECKING:
    from jobbot.bot.handlers import BotDeps

DEFAULT_LAST = 5
MAX_LAST = 20
LAST_WINDOW = timedelta(days=7)
MAX_BACKLOG = timedelta(days=7)
OWNER_ONLY_COMMANDS = ("check", "categories", "include", "exclude", "remove", "admin")


def friend_help(user: User) -> str:
    lines = [
        "<b>Команды</b>",
        "/last [n] — последние подходящие вакансии (по умолчанию 5)",
        "/pause — приостановить уведомления",
        "/resume — возобновить",
        "/status — состояние",
        "/filters — " + ("настроить фильтры" if user.can_edit else "мои фильтры"),
    ]
    return "\n".join(lines)


def create_friends_router(deps: BotDeps) -> Router:
    router = Router(name="friends")
    router.message.filter(Role("friend"))
    storage = deps.storage
    titles = {c.key: c.title for c in deps.settings.categories}

    async def reply(message: Message, text: str, **kwargs: object) -> None:
        await message.answer(text, parse_mode=ParseMode.HTML, **kwargs)  # type: ignore[arg-type]

    @router.message(Command("start"))
    async def cmd_start(message: Message, friend: User) -> None:
        summary = await profile_summary(storage, friend.profile, keywords=False)
        text = (
            f"👋 Привет{', ' + friend.name if friend.name else ''}! Я присылаю новые вакансии "
            "с somon.tj по вашим фильтрам — сразу, как они появляются.\n\n"
            f"{summary}\n\n{friend_help(friend)}"
        )
        await reply(message, text, reply_markup=filters_reply_keyboard())

    @router.message(Command("help"))
    async def cmd_help(message: Message, friend: User) -> None:
        await reply(message, friend_help(friend))

    @router.message(Command("status"))
    async def cmd_status(message: Message, friend: User) -> None:
        week = await storage.count_delivered_since(
            friend.chat_id, datetime.now(UTC) - timedelta(days=7)
        )
        state = "⏸ на паузе" if friend.status == "paused" else "✅ включены"
        await reply(
            message,
            f"📊 <b>Статус</b>\nУведомления: {state}\nОтправлено за 7 дней: {week}",
        )

    @router.message(Command("pause"))
    async def cmd_pause(message: Message, friend: User) -> None:
        if friend.status == "paused":
            await reply(message, "Уже на паузе. Возобновить: /resume")
            return
        await storage.set_user_status(friend.chat_id, "paused")
        await reply(message, "⏸ Уведомления приостановлены. Возобновить: /resume")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message, friend: User) -> None:
        if friend.status != "paused":
            await reply(message, "Уведомления и так включены.")
            return
        await storage.set_user_status(friend.chat_id, "active")
        now = datetime.now(UTC)
        since = now - timedelta(hours=24)
        if friend.paused_at:
            since = max(from_iso(friend.paused_at), now - MAX_BACKLOG)
        await reply(message, "▶️ Уведомления возобновлены.")
        await deps.poller.flush_friend(friend.chat_id, since)

    @router.message(Command("last"))
    async def cmd_last(message: Message, command: CommandObject, friend: User) -> None:
        count = DEFAULT_LAST
        if command.args:
            try:
                count = int(command.args.strip())
            except ValueError:
                await reply(message, f"❌ Нужно число от 1 до {MAX_LAST}: /last 10")
                return
            if not 1 <= count <= MAX_LAST:
                await reply(message, f"❌ Число должно быть от 1 до {MAX_LAST}.")
                return
        ads = await matching_for(storage, friend.chat_id, LAST_WINDOW)
        if not ads:
            await reply(message, "За последние 7 дней подходящих вакансий нет.")
            return
        await deps.notifier.send_ads(ads[:count], titles, chat_id=friend.chat_id)

    @router.message(Command(*OWNER_ONLY_COMMANDS))
    async def owner_only(message: Message) -> None:
        await reply(message, "Эта команда доступна только владельцу бота.")

    @router.message()
    async def anything_else(message: Message) -> None:
        if message.text == FILTERS_BUTTON:
            return  # обрабатывает меню фильтров
        await reply(message, "Я понимаю только команды. Список: /help")

    return router
