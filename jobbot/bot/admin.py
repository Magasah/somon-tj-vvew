"""Панель владельца /admin (v1.2): друзья — добавить, настроить фильтры, пауза, удалить.

Добавить друга можно:
- по числовому ID (бот сразу начнёт присылать вакансии, если друг уже нажимал /start);
- по @username (приглашение: друг откроет бота — и сразу получит доступ);
- пересылкой любого сообщения друга;
- кнопкой «✅ Добавить» в заявке, которая приходит, когда незнакомец пишет боту.

Фильтры друга (категории, город, зарплата, график, стаж) настраивает владелец через то же
меню фильтров; переключатель «Сам меняет фильтры» разрешает другу менять всё, кроме категорий.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from jobbot.bot.filters_menu import (
    FilterCB,
    profile_summary,
    short_hash,
)
from jobbot.bot.middleware import Role
from jobbot.filters import FilterProfile
from jobbot.storage import User, normalize_username

if TYPE_CHECKING:
    from jobbot.bot.handlers import BotDeps

log = logging.getLogger("bot.admin")

MENU_TTL = timedelta(hours=24)
USERNAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")
STATUS_ICON = {"active": "✅", "paused": "⏸", "blocked": "🚫"}
STATUS_TEXT = {
    "active": "✅ получает вакансии",
    "paused": "⏸ на паузе",
    "blocked": "🚫 заблокировал бота",
}
WELCOME_TEXT = (
    "👋 Вам открыли доступ к боту вакансий somon.tj — новые подходящие вакансии будут приходить "
    "сюда. Нажмите /start."
)


class AdminCB(CallbackData, prefix="adm"):
    """callback_data панели: действие, chat_id пользователя/заявки, хеш @username."""

    a: str
    u: int = 0
    h: str = ""


class AdminInput(StatesGroup):
    add = State()


Screen = tuple[str, InlineKeyboardMarkup | None]


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def button(text: str, action: str, u: int = 0, h: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=AdminCB(a=action, u=u, h=h).pack())


def parse_friend_ref(text: str) -> tuple[int | None, str | None]:
    """Ввод владельца → (chat_id, None) или (None, username). Неверно → ValueError."""
    value = text.strip()
    digits = value.lstrip("-")
    if digits.isdigit():
        chat_id = int(value)
        if chat_id <= 0:
            raise ValueError("Нужен ID человека (положительное число), а не группы.")
        return chat_id, None
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/")
    username = normalize_username(value)
    if not username or not 5 <= len(username) <= 32 or set(username) - USERNAME_CHARS:
        raise ValueError(
            "Не понял. Пришлите числовой ID (например 123456789), @username "
            "или перешлите сюда любое сообщение друга."
        )
    return None, username


def create_admin_router(deps: BotDeps) -> Router:
    router = Router(name="admin")
    router.message.filter(Role("owner"))
    router.callback_query.filter(Role("owner"))
    storage = deps.storage

    # ---------- экраны ----------

    async def home_screen() -> Screen:
        users = await storage.list_users()
        invites = await storage.list_invites()
        requests = await storage.list_requests()
        lines = ["👥 <b>Пользователи бота</b>", ""]
        if not users and not invites:
            lines.append("Пока только вы. Добавьте друга — кнопка ниже.")
        rows: list[list[InlineKeyboardButton]] = []
        for user in users:
            rows.append([button(f"{STATUS_ICON[user.status]} {user.title}", "usr", user.chat_id)])
        for username in invites:
            rows.append([button(f"⏳ @{username} (ждём /start)", "inv", h=short_hash(username))])
        if users or invites:
            lines.append("Нажмите на человека, чтобы настроить его фильтры.")
        rows.append([button("➕ Добавить друга", "add")])
        rows.append([button(f"🙋 Заявки ({len(requests)})", "req")])
        rows.append([button("✖️ Закрыть", "close")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    async def user_screen(user: User) -> Screen:
        week = await storage.count_delivered_since(
            user.chat_id, datetime.now(UTC) - timedelta(days=7)
        )
        lines = [
            f"👤 <b>{esc(user.title)}</b>",
            f"ID: <code>{user.chat_id}</code>",
            f"Статус: {STATUS_TEXT[user.status]}",
            f"Отправлено за 7 дней: {week}",
            "",
            await profile_summary(storage, user.profile, keywords=False),
            "",
            "Свои фильтры: "
            + ("друг может менять сам (кроме категорий)" if user.can_edit else "меняете только вы"),
        ]
        edit_toggle = "🔓 Сам меняет фильтры: да" if user.can_edit else "🔒 Сам меняет фильтры: нет"
        pause = (
            button("▶️ Возобновить", "res", user.chat_id)
            if user.status != "active"
            else button("⏸ Пауза", "pau", user.chat_id)
        )
        rows = [
            [
                InlineKeyboardButton(
                    text="⚙️ Фильтры друга",
                    callback_data=FilterCB(a="home", u=user.chat_id).pack(),
                )
            ],
            [button(edit_toggle, "edt", user.chat_id)],
            [pause, button("🗑 Удалить", "del", user.chat_id)],
            [button("⬅️ К списку", "home")],
        ]
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    def delete_screen(user: User) -> Screen:
        text = (
            f"🗑 Удалить <b>{esc(user.title)}</b>?\n"
            "Вакансии ему больше не будут приходить. Добавить снова можно в любой момент."
        )
        rows = [[button("Да, удалить", "dly", user.chat_id), button("Отмена", "usr", user.chat_id)]]
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    def invite_screen(username: str) -> Screen:
        text = (
            f"⏳ <b>@{esc(username)}</b> приглашён.\n"
            "Как только он откроет бота и нажмёт /start, доступ откроется сам, а вам придёт "
            "сообщение. Ссылку на бота можно переслать ему."
        )
        rows = [
            [button("🗑 Отменить приглашение", "ind", h=short_hash(username))],
            [button("⬅️ К списку", "home")],
        ]
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def requests_screen() -> Screen:
        requests = await storage.list_requests()
        lines = ["🙋 <b>Заявки</b> — люди, которые написали боту", ""]
        rows: list[list[InlineKeyboardButton]] = []
        if not requests:
            lines.append("Новых заявок нет.")
        for request in requests[:20]:
            who = esc(request.name or "Без имени")
            if request.username:
                who += f" (@{esc(request.username)})"
            lines.append(f"• {who} — ID <code>{request.chat_id}</code>")
            rows.append(
                [
                    button(f"✅ {request.name or request.chat_id}", "rqa", request.chat_id),
                    button("🚫", "rqd", request.chat_id),
                ]
            )
        rows.append([button("⬅️ К списку", "home")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    def add_screen(error: str | None = None) -> Screen:
        text = (
            "➕ <b>Добавить друга</b>\n\n"
            "Пришлите одно из:\n"
            "• числовой ID (например <code>123456789</code>) — его покажет @userinfobot;\n"
            "• @username друга;\n"
            "• или перешлите сюда любое сообщение от друга.\n\n"
            "Проще всего: пусть друг напишет боту — вам придёт заявка с кнопкой «Добавить»."
        )
        if error:
            text = f"❌ {esc(error)}\n\n{text}"
        rows = [[button("✖️ Отмена", "home")]]
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    # ---------- вывод ----------

    async def show(message: Message, screen: Screen) -> None:
        text, keyboard = screen
        try:
            await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise

    async def show_by_id(bot: Bot, chat_id: int, message_id: int, screen: Screen) -> None:
        text, keyboard = screen
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise

    async def welcome(bot: Bot, chat_id: int) -> bool:
        """Сообщить другу, что доступ открыт. False — Telegram не дал (друг ещё не писал боту)."""
        try:
            await bot.send_message(chat_id, WELCOME_TEXT)
        except TelegramAPIError:
            return False
        return True

    async def add_by_id(
        bot: Bot, chat_id: int, username: str | None = None, name: str | None = None
    ) -> tuple[User | None, str]:
        """Добавить друга по chat_id. Возвращает (пользователь, пояснение для владельца)."""
        if chat_id == deps.settings.owner_chat_id:
            return None, "Это ваш собственный ID — вы и так владелец."
        request = await storage.get_request(chat_id)
        username = username or (request.username if request else None)
        name = name or (request.name if request else None)
        added = await storage.add_user(
            chat_id, username=username, name=name, profile=default_friend_profile()
        )
        user = await storage.get_user(chat_id)
        if not added:
            return user, "Этот человек уже добавлен."
        greeted = await welcome(bot, chat_id)
        note = (
            "Друг добавлен и получил сообщение."
            if greeted
            else "Друг добавлен. Пусть откроет бота и нажмёт /start — сам бот не может "
            "написать первым."
        )
        log.info("владелец добавил пользователя %d", chat_id)
        return user, note + " Теперь настройте ему фильтры."

    def default_friend_profile() -> FilterProfile:
        """Новый друг получает все видимые разделы; владелец сразу сужает их в карточке."""
        return FilterProfile(categories=list(deps.settings.visible_keys))

    # ---------- команды ----------

    @router.message(Command("admin"))
    async def cmd_admin(message: Message, state: FSMContext) -> None:
        await state.clear()
        text, keyboard = await home_screen()
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @router.message(StateFilter(AdminInput.add), ~F.text.startswith("/"))
    async def input_friend(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        menu_id = data.get("menu_id")
        chat_id: int | None
        username: str | None = None
        name: str | None = None
        origin = message.forward_origin
        if isinstance(origin, MessageOriginUser):
            chat_id = origin.sender_user.id
            username = origin.sender_user.username
            name = origin.sender_user.full_name
        elif isinstance(origin, MessageOriginHiddenUser):
            await show_by_id(
                bot,
                message.chat.id,
                menu_id,
                add_screen(
                    "Друг скрыл аккаунт в пересылке. Пришлите его ID или @username, "
                    "или попросите его написать боту — придёт заявка."
                ),
            )
            return
        else:
            try:
                chat_id, username = parse_friend_ref(message.text or "")
            except ValueError as exc:
                await show_by_id(bot, message.chat.id, menu_id, add_screen(str(exc)))
                return

        if chat_id is None and username is not None:
            existing = await storage.find_user_by_username(username)
            request = await storage.find_request_by_username(username)
            if existing is not None:
                chat_id = existing.chat_id
            elif request is not None:
                chat_id, name = request.chat_id, request.name
            else:
                await storage.add_invite(username)
                await state.clear()
                await show_by_id(bot, message.chat.id, menu_id, invite_screen(username))
                await message.answer(
                    f"⏳ @{esc(username)} приглашён. Пусть откроет бота и нажмёт /start.",
                    parse_mode=ParseMode.HTML,
                )
                return

        if chat_id is None:  # сюда не доходим: выше chat_id задан во всех ветках
            return
        user, note = await add_by_id(bot, chat_id, username, name)
        if user is None:
            await show_by_id(bot, message.chat.id, menu_id, add_screen(note))
            return
        await state.clear()
        await show_by_id(bot, message.chat.id, menu_id, await user_screen(user))
        await message.answer(note)

    # ---------- кнопки ----------

    @router.callback_query(AdminCB.filter())
    async def on_button(
        query: CallbackQuery, callback_data: AdminCB, state: FSMContext, bot: Bot
    ) -> None:
        message = query.message
        if not isinstance(message, Message) or datetime.now(UTC) - message.date > MENU_TTL:
            await query.answer("Меню устарело, откройте /admin", show_alert=True)
            return
        action, u, h = callback_data.a, callback_data.u, callback_data.h

        if action == "home":
            await state.clear()
            await show(message, await home_screen())
        elif action == "close":
            await state.clear()
            await show(message, ("👥 Панель закрыта. Открыть снова: /admin", None))
        elif action == "add":
            await state.set_state(AdminInput.add)
            await state.update_data(menu_id=message.message_id)
            await show(message, add_screen())
        elif action == "req":
            await show(message, await requests_screen())
        elif action == "rqa":
            if await storage.get_request(u) is None and await storage.get_user(u) is None:
                return await reject(query, action, u)
            user, note = await add_by_id(bot, u)
            if user is None:
                await query.answer(note, show_alert=True)
                return None
            await show(message, await user_screen(user))
            await query.answer(note[:190], show_alert=True)
            return None
        elif action == "rqd":
            if await storage.get_request(u) is None:
                return await reject(query, action, u)
            await storage.decline_request(u)
            await show(message, await requests_screen())
            await query.answer("Заявка отклонена")
            return None
        elif action in ("usr", "edt", "pau", "res", "del", "dly"):
            user = await storage.get_user(u)
            if user is None:
                await query.answer("Такого пользователя уже нет", show_alert=True)
                await show(message, await home_screen())
                return None
            if action == "edt":
                await storage.set_user_can_edit(u, not user.can_edit)
            elif action == "pau":
                await storage.set_user_status(u, "paused")
            elif action == "res":
                await storage.set_user_status(u, "active")
                await deps.poller.flush_friend(u)
            elif action == "del":
                await show(message, delete_screen(user))
                await query.answer()
                return None
            elif action == "dly":
                await storage.delete_user(u)
                log.info("владелец удалил пользователя %d", u)
                await show(message, await home_screen())
                await query.answer("Удалён")
                return None
            fresh = await storage.get_user(u)
            if fresh is not None:
                await show(message, await user_screen(fresh))
        elif action in ("inv", "ind"):
            username = next((n for n in await storage.list_invites() if short_hash(n) == h), None)
            if username is None:
                await query.answer("Приглашения уже нет", show_alert=True)
                await show(message, await home_screen())
                return None
            if action == "inv":
                await show(message, invite_screen(username))
            else:
                await storage.delete_invite(username)
                await show(message, await home_screen())
                await query.answer("Приглашение отменено")
                return None
        else:
            return await reject(query, action, u)
        await query.answer()
        return None

    @router.callback_query(F.data.startswith("adm"))
    async def on_broken_button(query: CallbackQuery) -> None:
        log.warning("неразборчивая кнопка панели: %r", (query.data or "")[:64])
        await query.answer("Неизвестная кнопка, откройте /admin", show_alert=True)

    async def reject(query: CallbackQuery, action: str, u: int) -> None:
        log.warning("отклонена кнопка панели: действие=%r u=%r", action[:16], u)
        await query.answer("Неизвестная кнопка, откройте /admin", show_alert=True)

    return router
