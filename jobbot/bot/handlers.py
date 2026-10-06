"""Команды бота (раздел 3.5 ТЗ). Доступ ограничен middleware — сюда доходит только владелец."""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from jobbot.bot.middleware import OwnerOnlyMiddleware
from jobbot.config import Settings
from jobbot.matcher import MAX_WORDS_PER_LIST, validate_keyword
from jobbot.notifier import Notifier
from jobbot.poller import (
    STATE_BLOCKED_UNTIL,
    STATE_LAST_ERROR,
    STATE_LAST_POLL,
    STATE_PAUSED,
    STATE_PAUSED_AT,
    Poller,
)
from jobbot.storage import CategoryState, KeywordKind, Storage, from_iso, to_iso

log = logging.getLogger("bot.handlers")

CHECK_COOLDOWN_SEC = 60  # /check не чаще раза в минуту
MAX_PAUSE_BACKLOG = timedelta(days=7)  # сколько накопленного за паузу досылаем при /resume
DEFAULT_LAST = 5
MAX_LAST = 20
MAX_TEXT_LEN = 4000

MODE_HELP = (
    "<b>all</b> — все вакансии раздела, кроме слов из exclude;\n"
    "<b>keywords</b> — только те, где есть слово из include (и нет слов из exclude)."
)

HELP_TEXT = (
    "<b>Команды</b>\n"
    "/status — состояние бота\n"
    "/check — проверить сайт прямо сейчас\n"
    "/pause — приостановить уведомления\n"
    "/resume — возобновить уведомления\n"
    "/filters — слова фильтра и режимы разделов\n"
    "/include <i>слово</i> — добавить слово в include\n"
    "/exclude <i>слово</i> — добавить слово в exclude\n"
    "/remove <i>слово</i> — удалить слово из обоих списков\n"
    "/categories — включить/выключить разделы, режим all/keywords\n"
    "/last [n] — последние n подходящих вакансий (1–20, по умолчанию 5)"
)

START_TEXT = (
    "👋 Привет! Я слежу за новыми вакансиями на somon.tj и сразу присылаю подходящие.\n\n"
    + HELP_TEXT
)


@dataclass
class BotDeps:
    """Всё, что нужно командам: настройки, база, опрос, отправка."""

    settings: Settings
    storage: Storage
    poller: Poller
    notifier: Notifier
    last_check_at: float | None = None  # monotonic-время последнего /check


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def clip(text: str) -> str:
    """Укоротить текст под лимит Telegram."""
    return text if len(text) <= MAX_TEXT_LEN else text[: MAX_TEXT_LEN - 1] + "…"


def format_local(iso_value: str | None, tz_name: str) -> str:
    """Время из базы (UTC) → строка в часовом поясе владельца."""
    if not iso_value:
        return "ещё не было"
    moment = from_iso(iso_value).astimezone(ZoneInfo(tz_name))
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def resume_since(paused_at: str | None, now: datetime) -> datetime:
    """С какого момента досылать накопленное при /resume: с начала паузы, но не глубже 7 дней."""
    floor = now - MAX_PAUSE_BACKLOG
    if not paused_at:
        return now - timedelta(hours=24)
    return max(from_iso(paused_at), floor)


def create_router(deps: BotDeps) -> Router:
    router = Router(name="owner-commands")
    storage, settings = deps.storage, deps.settings
    titles = {c.key: c.title for c in settings.categories}

    async def reply(message: Message, text: str, **kwargs: object) -> None:
        await message.answer(clip(text), parse_mode=ParseMode.HTML, **kwargs)  # type: ignore[arg-type]

    @router.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        await reply(message, START_TEXT)

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await reply(message, HELP_TEXT)

    # ---------- статус и управление ----------

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        tz = settings.tz
        paused = await storage.get_state(STATE_PAUSED) == "1"
        local_midnight = datetime.now(ZoneInfo(tz)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        sent_today = await storage.count_sent_since(local_midnight.astimezone(UTC))
        lines = [
            "📊 <b>Статус</b>",
            f"Уведомления: {'⏸ на паузе' if paused else '✅ включены'}",
            f"Последний опрос: {format_local(await storage.get_state(STATE_LAST_POLL), tz)}",
            f"Объявлений в базе: {await storage.count_ads()}",
            f"Отправлено сегодня: {sent_today}",
        ]
        blocked_until = await storage.get_state(STATE_BLOCKED_UNTIL)
        if blocked_until and blocked_until > to_iso():
            lines.append(
                f"🚫 Сайт ограничил доступ, опрос на паузе до {format_local(blocked_until, tz)}"
            )
        last_error = await storage.get_state(STATE_LAST_ERROR)
        if last_error:
            stamp, _, text = last_error.partition(" ")
            lines.append(f"Последняя ошибка: {format_local(stamp, tz)} — {esc(text)}")
        else:
            lines.append("Последняя ошибка: нет")
        await reply(message, "\n".join(lines))

    @router.message(Command("check"))
    async def cmd_check(message: Message) -> None:
        if deps.poller.busy:
            await reply(message, "Проверка уже идёт.")
            return
        now = time.monotonic()
        if deps.last_check_at is not None and now - deps.last_check_at < CHECK_COOLDOWN_SEC:
            wait = int(CHECK_COOLDOWN_SEC - (now - deps.last_check_at)) + 1
            await reply(message, f"Слишком часто. Повторите через {wait} с.")
            return
        deps.last_check_at = now
        await reply(message, "🔎 Проверяю сайт…")
        stats = await deps.poller.poll_once()
        if stats is None:  # опрос успел стартовать из основного цикла
            await reply(message, "Проверка уже идёт.")
        elif stats.skipped:
            await reply(
                message, "Опрос на паузе: сайт временно ограничил доступ. Подробности — /status."
            )
        else:
            parts = [
                f"{esc(titles.get(c.key, c.key))}: найдено {c.found}, новых {c.new}, "
                f"отправлено {c.sent}" + (f" (ошибка: {esc(c.error)})" if c.error else "")
                for c in stats.categories
            ]
            await reply(message, "✅ Проверка завершена.\n" + "\n".join(parts))

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        if await storage.get_state(STATE_PAUSED) == "1":
            await reply(message, "Уже на паузе. Возобновить: /resume")
            return
        await storage.set_state(STATE_PAUSED, "1")
        await storage.set_state(STATE_PAUSED_AT, to_iso())
        await reply(
            message,
            "⏸ Уведомления приостановлены. Опрос продолжается, новые вакансии копятся.\n"
            "Возобновить: /resume",
        )

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        if await storage.get_state(STATE_PAUSED) != "1":
            await reply(message, "Уведомления и так включены.")
            return
        paused_at = await storage.get_state(STATE_PAUSED_AT)
        await storage.set_state(STATE_PAUSED, "0")
        since = resume_since(paused_at, datetime.now(UTC))
        backlog = len(await storage.unsent_matched(since))
        suffix = f" Накопленных вакансий: {backlog}, отправляю." if backlog else " Накопленных нет."
        await reply(message, "▶️ Уведомления возобновлены." + suffix)
        if backlog:
            await deps.poller.flush_unsent(since=since)

    # ---------- фильтры ----------

    @router.message(Command("filters"))
    async def cmd_filters(message: Message) -> None:
        include = await storage.get_keywords("include")
        exclude = await storage.get_keywords("exclude")
        lines = [
            f"✅ <b>include</b> ({len(include)}): {esc(', '.join(include)) or '—'}",
            f"🚫 <b>exclude</b> ({len(exclude)}): {esc(', '.join(exclude)) or '—'}",
            "",
            "<b>Разделы</b>",
        ]
        for cat in await storage.get_categories():
            state = "вкл" if cat.enabled else "выкл"
            lines.append(f"• {esc(titles.get(cat.key, cat.key))} — {state}, режим {cat.mode}")
        await reply(message, "\n".join(lines))

    async def add_word(message: Message, command: CommandObject, kind: KeywordKind) -> None:
        name = "include" if kind == "include" else "exclude"
        if not command.args:
            await reply(message, f"Укажите слово: /{name} <i>слово</i>")
            return
        try:
            word = validate_keyword(command.args)
        except ValueError as exc:
            await reply(message, f"❌ {esc(exc)}")
            return
        if await storage.count_keywords(kind) >= MAX_WORDS_PER_LIST:
            await reply(
                message,
                f"❌ В списке {name} уже {MAX_WORDS_PER_LIST} слов — это максимум.\n"
                "Удалите ненужные: /remove",
            )
            return
        added = await storage.add_keyword(kind, word)
        if added:
            await reply(message, f"✅ Слово «{esc(word)}» добавлено в {name}.")
        else:
            await reply(message, f"Слово «{esc(word)}» уже есть в {name}.")

    @router.message(Command("include"))
    async def cmd_include(message: Message, command: CommandObject) -> None:
        await add_word(message, command, "include")

    @router.message(Command("exclude"))
    async def cmd_exclude(message: Message, command: CommandObject) -> None:
        await add_word(message, command, "exclude")

    @router.message(Command("remove"))
    async def cmd_remove(message: Message, command: CommandObject) -> None:
        if not command.args:
            await reply(message, "Укажите слово: /remove <i>слово</i>")
            return
        try:
            word = validate_keyword(command.args)
        except ValueError as exc:
            await reply(message, f"❌ {esc(exc)}")
            return
        removed = await storage.remove_keyword(word)
        if removed:
            await reply(message, f"🗑 Слово «{esc(word)}» удалено.")
        else:
            await reply(message, f"Слова «{esc(word)}» нет ни в include, ни в exclude.")

    # ---------- разделы ----------

    async def categories_view() -> tuple[str, InlineKeyboardMarkup]:
        rows: list[list[InlineKeyboardButton]] = []
        for cat in await storage.get_categories():
            title = titles.get(cat.key, cat.key)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{'✅' if cat.enabled else '⬜'} {title}",
                        callback_data=f"cat:t:{cat.key}",
                    ),
                    InlineKeyboardButton(
                        text=f"Режим: {cat.mode}", callback_data=f"cat:m:{cat.key}"
                    ),
                ]
            )
        text = (
            "<b>Разделы</b>\n"
            "Нажмите на название — раздел включится или выключится, на «Режим» — переключится "
            "all/keywords.\n\n" + MODE_HELP
        )
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    @router.message(Command("categories"))
    async def cmd_categories(message: Message) -> None:
        text, keyboard = await categories_view()
        await reply(message, text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("cat:"))
    async def on_category_button(query: CallbackQuery) -> None:
        parts = (query.data or "").split(":")
        states: dict[str, CategoryState] = {c.key: c for c in await storage.get_categories()}
        if len(parts) != 3 or parts[2] not in states:
            await query.answer("Неизвестный раздел", show_alert=True)
            return
        action, key = parts[1], parts[2]
        current = states[key]
        if action == "t":
            try:
                await storage.set_category_enabled(key, not current.enabled)
            except ValueError as exc:  # последний выбранный раздел выключить нельзя
                await query.answer(str(exc), show_alert=True)
                return
        elif action == "m":
            await storage.set_category_mode(key, "keywords" if current.mode == "all" else "all")
        else:
            await query.answer("Неизвестное действие", show_alert=True)
            return
        text, keyboard = await categories_view()
        if isinstance(query.message, Message):
            await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        await query.answer("Готово")

    # ---------- последние вакансии ----------

    @router.message(Command("last"))
    async def cmd_last(message: Message, command: CommandObject) -> None:
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
        ads = await storage.recent_matched(count)
        if not ads:
            await reply(message, "В базе пока нет подходящих вакансий.")
            return
        await deps.notifier.send_ads(ads, titles)

    return router


def build_dispatcher(deps: BotDeps) -> Dispatcher:
    """Dispatcher с проверкой владельца (до любых хендлеров) и всеми командами."""
    owner_id = deps.settings.owner_chat_id
    if owner_id is None:
        raise ValueError("OWNER_CHAT_ID не задан")
    dispatcher = Dispatcher()
    guard = OwnerOnlyMiddleware(owner_id)
    dispatcher.message.outer_middleware(guard)
    dispatcher.callback_query.outer_middleware(guard)
    dispatcher.include_router(create_router(deps))
    return dispatcher
