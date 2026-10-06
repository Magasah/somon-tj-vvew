"""Меню фильтров на кнопках (v1.1, раздел 7 TZ_update_filters.md).

Одно сообщение, которое редактируется на месте. Каждое нажатие сразу сохраняет профиль через
`Storage.update_profile` (под блокировкой, в одной транзакции). Данным из callback не верим:
действие проверяется по списку, значения — по короткому хешу среди текущих вариантов.
"""

from __future__ import annotations

import hashlib
import html
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)
from pydantic import ValidationError

from jobbot.catalog import CATALOG_BY_KEY
from jobbot.filters import (
    SALARY_INPUT_HELP,
    FilterProfile,
    SalaryFilter,
    parse_salary_input,
)
from jobbot.matcher import MAX_WORDS_PER_LIST, validate_keyword
from jobbot.models import Ad
from jobbot.normalize import normalize_city, normalize_text
from jobbot.selection import matches_profile
from jobbot.storage import AttrName, KeywordKind

if TYPE_CHECKING:
    from jobbot.bot.handlers import BotDeps

log = logging.getLogger("bot.filters")

FILTERS_BUTTON = "⚙️ Фильтры"
MENU_TTL = timedelta(hours=24)
MAX_TEXT_LEN = 4000
STARTER_CITIES = ("Душанбе", "Худжанд", "Кулоб", "Бохтар", "Восе", "Истаравшан")
MIN_KNOWN_CITIES = 6
MAX_CITY_BUTTONS = 12
MAX_VALUE_BUTTONS = 15
SALARY_PRESETS: tuple[int | None, ...] = (None, 1000, 2000, 3000, 5000, 8000)
CHECK_WINDOW = timedelta(days=7)
CHECK_SHOW = 5
SEND_ALL_WINDOW = timedelta(hours=24)
SEND_ALL_LIMIT = 20
MAX_DELETE_BUTTONS = 30
VALUES_HINT = "Значения берутся с сайта. Новые варианты появятся по мере новых вакансий."
SITE_ANY_EXPERIENCE = "любой"  # так сайт пишет стаж, когда работодателю он не важен


class FilterCB(CallbackData, prefix="flt"):
    """callback_data меню: действие + короткий аргумент (ключ, индекс или хеш), ≤ 64 байт."""

    a: str
    h: str = ""


class FilterInput(StatesGroup):
    salary = State()
    include = State()
    exclude = State()


@dataclass(frozen=True, slots=True)
class Option:
    value: str  # нормализованное значение (как в профиле)
    label: str  # подпись на кнопке


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT_LEN else text[: MAX_TEXT_LEN - 1] + "…"


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def plural_vacancies(count: int) -> str:
    if count % 100 in range(11, 15):
        return "вакансий"
    return {1: "вакансия", 2: "вакансии", 3: "вакансии", 4: "вакансии"}.get(count % 10, "вакансий")


def salary_range_text(salary: SalaryFilter) -> str:
    if salary.min is not None and salary.max is not None:
        return f"{money(salary.min)}–{money(salary.max)} c."
    if salary.min is not None:
        return f"от {money(salary.min)} c."
    if salary.max is not None:
        return f"до {money(salary.max)} c."
    return "Любая"


def filters_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная кнопка под полем ввода."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=FILTERS_BUTTON)]], resize_keyboard=True, is_persistent=True
    )


def button(text: str, action: str, arg: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=FilterCB(a=action, h=arg).pack())


def pairs(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def mark(selected: bool) -> str:
    return "✅" if selected else "▫️"


Screen = tuple[str, InlineKeyboardMarkup | None]


def create_filters_router(deps: BotDeps) -> Router:
    """Роутер меню фильтров (нужны settings, storage, notifier из `BotDeps`)."""
    router = Router(name="filters-menu")
    storage, settings = deps.storage, deps.settings

    # ---------- данные для экранов ----------

    def visible_keys() -> tuple[str, ...]:
        return settings.visible_keys

    async def value_options(attr: AttrName, selected: list[str], limit: int) -> list[Option]:
        """Варианты кнопок: частые значения с сайта + уже выбранные (чтобы их можно было снять)."""
        known = await storage.get_attr_values(attr, limit=limit)
        options = [Option(v.value_norm, v.label) for v in known]
        if attr == "city" and len(options) < MIN_KNOWN_CITIES:
            for label in STARTER_CITIES:
                value = normalize_city(label)
                if all(o.value != value for o in options):
                    options.append(Option(value, label))
        labels = {o.value: o.label for o in options}
        for value in selected:
            if value not in labels:
                options.append(Option(value, value.capitalize()))
        return options

    async def labels_for(attr: AttrName, values: list[str]) -> list[str]:
        known = {v.value_norm: v.label for v in await storage.get_attr_values(attr, limit=200)}
        return [experience_label(known.get(v, v.capitalize()), v, attr) for v in values]

    def experience_label(label: str, value: str, attr: AttrName) -> str:
        if attr == "experience" and value == SITE_ANY_EXPERIENCE:
            return "Любой стаж (так на сайте)"
        return label

    async def matching_ads(window: timedelta) -> list[Ad]:
        profile = await storage.get_filter_profile()
        include = await storage.get_keywords("include")
        exclude = await storage.get_keywords("exclude")
        modes = {c.key: c.mode for c in await storage.get_categories()}
        items = await storage.ads_since(datetime.now(UTC) - window)
        return [
            item.ad
            for item in items
            if matches_profile(
                item,
                profile,
                keyword_mode=modes.get(item.ad.category_key, "all"),
                include=include,
                exclude=exclude,
            )
        ]

    async def summary(profile: FilterProfile) -> str:
        categories = " · ".join(
            esc(CATALOG_BY_KEY[k].title) for k in profile.categories if k in CATALOG_BY_KEY
        )
        cities = ", ".join(map(esc, await labels_for("city", profile.cities))) or "Все"
        schedules = ", ".join(map(esc, await labels_for("schedule", profile.schedules))) or "Любой"
        experience = (
            ", ".join(map(esc, await labels_for("experience", profile.experience))) or "Любой"
        )
        negotiable = "показывать" if profile.salary.include_negotiable else "скрывать"
        include = await storage.get_keywords("include")
        exclude = await storage.get_keywords("exclude")
        words = " · ".join(
            part for part in (word_list("+", include), word_list("−", exclude)) if part
        )
        unknown = "показывать" if profile.include_unknown_attrs else "скрывать"
        return (
            f"🗂 Категории: {categories}\n"
            f"📍 Город: {cities}\n"
            f"💰 Зарплата: {salary_range_text(profile.salary)} · «Договорная» — {negotiable}\n"
            f"🕒 График: {schedules}\n"
            f"🎓 Стаж: {experience}\n"
            f"🔤 Слова: {words or 'нет'}\n"
            f"❔ Если поле не указано: {unknown}"
        )

    def word_list(sign: str, words: list[str], limit: int = 6) -> str:
        shown = ", ".join(f"{sign}{esc(w)}" for w in words[:limit])
        if len(words) > limit:
            shown += f" и ещё {len(words) - limit}"
        return shown

    # ---------- экраны ----------

    async def main_screen() -> Screen:
        profile = await storage.get_filter_profile()
        count = len(await matching_ads(CHECK_WINDOW))
        text = (
            "⚙️ <b>Ваши фильтры</b>\n\n"
            + await summary(profile)
            + f"\n\nПодходит за последние 7 дней: {count} {plural_vacancies(count)}"
        )
        keyboard = [
            [button("🗂 Категории", "cat"), button("📍 Город", "city")],
            [button("💰 Зарплата", "sal"), button("🕒 График", "sch")],
            [button("🎓 Стаж", "exp"), button("🔤 Ключевые слова", "kw")],
            [button("🔍 Проверить фильтр", "chk"), button("♻️ Сбросить", "rst")],
            [button("✅ Готово", "done")],
        ]
        return text, InlineKeyboardMarkup(inline_keyboard=keyboard)

    def back(target: str = "home") -> list[InlineKeyboardButton]:
        return [button("⬅️ Назад", target)]

    async def categories_screen() -> Screen:
        profile = await storage.get_filter_profile()
        rows = [
            [button(f"{mark(k in profile.categories)} {CATALOG_BY_KEY[k].title}", "ct", k)]
            for k in visible_keys()
        ]
        text = "🗂 <b>Категории</b>\nВыберите разделы, из которых присылать вакансии."
        return text, InlineKeyboardMarkup(inline_keyboard=[*rows, back()])

    async def city_screen() -> Screen:
        profile = await storage.get_filter_profile()
        options = await value_options("city", profile.cities, MAX_CITY_BUTTONS)
        selected = {normalize_city(c) for c in profile.cities}
        buttons = [
            button(f"{mark(o.value in selected)} {o.label}", "cy", short_hash(o.value))
            for o in options
        ]
        rows = [
            [button(f"{mark(not profile.cities)} 🌍 Все города", "ca")],
            *pairs(buttons),
            back(),
        ]
        text = "📍 <b>Город</b>\nМожно выбрать несколько. «Все города» снимает выбор."
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def salary_screen() -> Screen:
        profile = await storage.get_filter_profile()
        salary = profile.salary
        presets = []
        for index, amount in enumerate(SALARY_PRESETS):
            current = salary.max is None and salary.min == amount
            label = "Любая" if amount is None else f"от {money(amount)}"
            presets.append(button(f"{mark(current)} {label}", "sp", str(index)))
        negotiable = (
            "«Договорная»: показывать ✅"
            if salary.include_negotiable
            else "«Договорная»: скрывать ▫️"
        )
        rows = [
            *pairs(presets),
            [button("✏️ Ввести вручную", "sm")],
            [button(negotiable, "sn")],
            back(),
        ]
        text = (
            "💰 <b>Зарплата, c.</b>\n"
            f"Сейчас: {salary_range_text(salary)}\n"
            "Выберите нижнюю границу или введите диапазон вручную."
        )
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def values_screen(attr: AttrName) -> Screen:
        profile = await storage.get_filter_profile()
        selected = profile.schedules if attr == "schedule" else profile.experience
        options = await value_options(attr, selected, MAX_VALUE_BUTTONS)
        toggle, any_action = ("st", "sa") if attr == "schedule" else ("et", "ea")
        buttons = [
            button(
                f"{mark(o.value in selected)} {experience_label(o.label, o.value, attr)}",
                toggle,
                short_hash(o.value),
            )
            for o in options
        ]
        unknown = (
            "Если не указано: показывать ✅"
            if profile.include_unknown_attrs
            else "Если не указано: скрывать ▫️"
        )
        rows = [
            [button(f"{mark(not selected)} Любой", any_action)],
            *pairs(buttons),
            [button(unknown, "unk", attr[:3])],
            back(),
        ]
        title = "🕒 <b>График</b>" if attr == "schedule" else "🎓 <b>Стаж</b>"
        text = f"{title}\nМожно выбрать несколько.\n\n<i>{VALUES_HINT}</i>"
        if not options:
            text += "\n\nПока ни одного значения: они появятся после первых вакансий."
        if attr == "experience":
            text += "\n«Любой стаж» — работодателю опыт не важен."
        text += "\nЕсли выбрать график или стаж, бот будет открывать страницы вакансий (медленнее)."
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def keywords_screen() -> Screen:
        include = await storage.get_keywords("include")
        exclude = await storage.get_keywords("exclude")
        text = (
            "🔤 <b>Ключевые слова</b> (ищутся в названии вакансии)\n\n"
            f"➕ Нужные ({len(include)}): {esc(', '.join(include)) or '—'}\n"
            f"➖ Стоп-слова ({len(exclude)}): {esc(', '.join(exclude)) or '—'}\n\n"
            "Стоп-слово отклоняет вакансию всегда. Нужные слова работают в разделах "
            "с режимом keywords (/categories)."
        )
        rows = [
            [button("➕ Добавить нужное слово", "ki")],
            [button("➖ Добавить стоп-слово", "ke")],
            [button("🗑 Удалить слово", "kd")],
            back(),
        ]
        return clip(text), InlineKeyboardMarkup(inline_keyboard=rows)

    async def delete_words_screen() -> Screen:
        entries = await keyword_entries()
        buttons = [
            button(
                f"{'➕' if kind == 'include' else '➖'} {word}", "kx", short_hash(f"{kind}:{word}")
            )
            for kind, word in entries[:MAX_DELETE_BUTTONS]
        ]
        text = "🗑 <b>Удалить слово</b>\nНажмите на слово, чтобы удалить его."
        if not entries:
            text += "\n\nСписки пусты."
        if len(entries) > MAX_DELETE_BUTTONS:
            text += f"\nПоказаны первые {MAX_DELETE_BUTTONS}; остальные: /remove слово"
        return text, InlineKeyboardMarkup(inline_keyboard=[*pairs(buttons), back("kw")])

    async def keyword_entries() -> list[tuple[KeywordKind, str]]:
        include = await storage.get_keywords("include")
        exclude = await storage.get_keywords("exclude")
        return [("include", w) for w in include] + [("exclude", w) for w in exclude]

    async def check_screen() -> Screen:
        ads = await matching_ads(CHECK_WINDOW)
        if ads:
            lines = [f"🔍 <b>Подходит за 7 дней: {len(ads)}</b>. Последние:"]
            for ad in ads[:CHECK_SHOW]:
                details = " · ".join(esc(x) for x in (ad.salary_text, ad.city) if x)
                link = f'<a href="{html.escape(ad.url, quote=True)}">{esc(ad.title)}</a>'
                lines.append(f"• {link}" + (f" — {details}" if details else ""))
            text = "\n".join(lines)
        else:
            text = "🔍 За последние 7 дней подходящих вакансий нет. Попробуйте ослабить фильтры."
        rows = [[button("📨 Прислать все подходящие за 24 часа", "chs")], back()]
        return clip(text), InlineKeyboardMarkup(inline_keyboard=rows)

    def reset_screen() -> Screen:
        text = (
            "♻️ <b>Сбросить фильтры?</b>\n"
            "Категории, город, зарплата, график и стаж вернутся к начальным. "
            "Ключевые слова не изменятся."
        )
        rows = [[button("Да, сбросить", "rsy"), button("Отмена", "home")]]
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    def input_screen(kind: str, error: str | None = None) -> Screen:
        prompts = {
            "salary": f"✏️ Введите зарплату. {SALARY_INPUT_HELP}",
            "include": "➕ Напишите нужное слово (2–40 символов).",
            "exclude": "➖ Напишите стоп-слово (2–40 символов).",
        }
        text = prompts[kind]
        if error:
            text = f"❌ {esc(error)}\n\n{text}"
        text += "\n\nДля выхода — кнопка «Отмена» или /cancel."
        return text, InlineKeyboardMarkup(inline_keyboard=[[button("✖️ Отмена", "cx")]])

    # ---------- изменение профиля ----------

    async def mutate(query: CallbackQuery, change: Callable[[FilterProfile], None]) -> bool:
        try:
            await storage.update_profile(change)
        except ValidationError as exc:
            reason = exc.errors()[0]["msg"].removeprefix("Value error, ")
            message = reason[:1].upper() + reason[1:]
            await query.answer(message, show_alert=True)
            return False
        return True

    def toggle(values: list[str], value: str) -> list[str]:
        return [v for v in values if v != value] if value in values else [*values, value]

    # ---------- вывод на экран ----------

    async def show(message: Message, screen: Screen) -> None:
        text, keyboard = screen
        try:
            await message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
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
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise

    async def open_menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        text, keyboard = await main_screen()
        await message.answer(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    # ---------- команды и кнопка ----------

    @router.message(Command("filters"))
    async def cmd_filters(message: Message, state: FSMContext) -> None:
        await open_menu(message, state)

    @router.message(F.text == FILTERS_BUTTON)
    async def on_filters_button(message: Message, state: FSMContext) -> None:
        await open_menu(message, state)

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if await state.get_state() is None:
            await message.answer("Нечего отменять. Меню фильтров: /filters")
            return
        await state.clear()
        if data.get("menu_id"):
            await show_by_id(bot, message.chat.id, data["menu_id"], await screen_after(data))
        await message.answer("Ввод отменён.")

    async def screen_after(data: dict) -> Screen:
        return await salary_screen() if data.get("kind") == "salary" else await keywords_screen()

    # ---------- ввод текста (FSM) ----------

    not_command = F.text & ~F.text.startswith("/")

    @router.message(StateFilter(FilterInput.salary), not_command)
    async def input_salary(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        await try_delete(message)
        try:
            low, high = parse_salary_input(message.text or "")
        except ValueError as exc:
            await show_by_id(
                bot, message.chat.id, data["menu_id"], input_screen("salary", str(exc))
            )
            return

        def change(profile: FilterProfile) -> None:
            profile.salary = SalaryFilter(
                min=low, max=high, include_negotiable=profile.salary.include_negotiable
            )

        await storage.update_profile(change)
        await state.clear()
        await show_by_id(bot, message.chat.id, data["menu_id"], await salary_screen())

    @router.message(StateFilter(FilterInput.include, FilterInput.exclude), not_command)
    async def input_word(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        kind: KeywordKind = "include" if data.get("kind") == "include" else "exclude"
        await try_delete(message)
        try:
            word = validate_keyword(message.text or "")
            if await storage.count_keywords(kind) >= MAX_WORDS_PER_LIST:
                raise ValueError(f"В списке уже {MAX_WORDS_PER_LIST} слов — это максимум.")
        except ValueError as exc:
            await show_by_id(bot, message.chat.id, data["menu_id"], input_screen(kind, str(exc)))
            return
        await storage.add_keyword(kind, word)
        await state.clear()
        await show_by_id(bot, message.chat.id, data["menu_id"], await keywords_screen())

    async def try_delete(message: Message) -> None:
        """Убрать введённый текст из чата, чтобы не копились сообщения (не критично)."""
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

    # ---------- нажатия кнопок ----------

    @router.callback_query(FilterCB.filter())
    async def on_button(query: CallbackQuery, callback_data: FilterCB, state: FSMContext) -> None:
        message = query.message
        if not isinstance(message, Message) or datetime.now(UTC) - message.date > MENU_TTL:
            await query.answer("Меню устарело, откройте /filters", show_alert=True)
            return
        action, arg = callback_data.a, callback_data.h

        if action == "home":
            await state.clear()
            await show(message, await main_screen())
        elif action == "cat":
            await show(message, await categories_screen())
        elif action == "ct":
            if arg not in visible_keys():
                return await reject(query, action, arg)
            if not await mutate(
                query, lambda p: setattr(p, "categories", toggle(p.categories, arg))
            ):
                return None
            await show(message, await categories_screen())
        elif action == "city":
            await show(message, await city_screen())
        elif action == "ca":
            if not await mutate(query, lambda p: setattr(p, "cities", [])):
                return None
            await show(message, await city_screen())
        elif action == "cy":
            profile = await storage.get_filter_profile()
            option = find(await value_options("city", profile.cities, MAX_CITY_BUTTONS), arg)
            if option is None:
                return await stale_button(query, message, await city_screen())
            if not await mutate(
                query, lambda p: setattr(p, "cities", toggle(p.cities, option.value))
            ):
                return None
            await show(message, await city_screen())
        elif action == "sal":
            await show(message, await salary_screen())
        elif action == "sp":
            if not arg.isdigit() or int(arg) >= len(SALARY_PRESETS):
                return await reject(query, action, arg)
            amount = SALARY_PRESETS[int(arg)]

            def preset(p: FilterProfile) -> None:
                p.salary = SalaryFilter(min=amount, include_negotiable=p.salary.include_negotiable)

            if not await mutate(query, preset):
                return None
            await show(message, await salary_screen())
        elif action == "sn":

            def flip_negotiable(p: FilterProfile) -> None:
                p.salary = SalaryFilter(
                    min=p.salary.min,
                    max=p.salary.max,
                    include_negotiable=not p.salary.include_negotiable,
                )

            if not await mutate(query, flip_negotiable):
                return None
            await show(message, await salary_screen())
        elif action == "sm":
            await state.set_state(FilterInput.salary)
            await state.update_data(menu_id=message.message_id, kind="salary")
            await show(message, input_screen("salary"))
        elif action in ("sch", "exp"):
            await show(
                message, await values_screen("schedule" if action == "sch" else "experience")
            )
        elif action in ("st", "et"):
            attr: AttrName = "schedule" if action == "st" else "experience"
            field = "schedules" if attr == "schedule" else "experience"
            profile = await storage.get_filter_profile()
            option = find(
                await value_options(attr, getattr(profile, field), MAX_VALUE_BUTTONS), arg
            )
            if option is None:
                return await stale_button(query, message, await values_screen(attr))
            value = normalize_text(option.value)
            if not await mutate(
                query, lambda p: setattr(p, field, toggle(getattr(p, field), value))
            ):
                return None
            await show(message, await values_screen(attr))
        elif action in ("sa", "ea"):
            attr = "schedule" if action == "sa" else "experience"
            field = "schedules" if attr == "schedule" else "experience"
            if not await mutate(query, lambda p: setattr(p, field, [])):
                return None
            await show(message, await values_screen(attr))
        elif action == "unk":
            if arg not in ("sch", "exp"):
                return await reject(query, action, arg)
            if not await mutate(
                query, lambda p: setattr(p, "include_unknown_attrs", not p.include_unknown_attrs)
            ):
                return None
            await show(message, await values_screen("schedule" if arg == "sch" else "experience"))
        elif action == "kw":
            await state.clear()
            await show(message, await keywords_screen())
        elif action in ("ki", "ke"):
            kind = "include" if action == "ki" else "exclude"
            await state.set_state(FilterInput.include if kind == "include" else FilterInput.exclude)
            await state.update_data(menu_id=message.message_id, kind=kind)
            await show(message, input_screen(kind))
        elif action == "kd":
            await show(message, await delete_words_screen())
        elif action == "kx":
            entry = next(
                (e for e in await keyword_entries() if short_hash(f"{e[0]}:{e[1]}") == arg), None
            )
            if entry is None:
                return await stale_button(query, message, await delete_words_screen())
            await storage.remove_keyword_from(entry[0], entry[1])
            await show(message, await delete_words_screen())
        elif action == "cx":
            data = await state.get_data()
            await state.clear()
            await show(message, await screen_after(data))
        elif action == "chk":
            await show(message, await check_screen())
        elif action == "chs":
            await query.answer("Отправляю…")
            ads = await matching_ads(SEND_ALL_WINDOW)
            if not ads:
                await message.answer("За последние 24 часа подходящих вакансий нет.")
                return None
            await deps.notifier.send_digest(ads[:SEND_ALL_LIMIT])
            return None
        elif action == "rst":
            await show(message, reset_screen())
        elif action == "rsy":
            await storage.update_profile(lambda _p: FilterProfile())
            await show(message, await main_screen())
            await query.answer("Фильтры сброшены")
            return None
        elif action == "done":
            await state.clear()
            profile = await storage.get_filter_profile()
            await show(message, ("✅ <b>Фильтры сохранены</b>\n\n" + await summary(profile), None))
        else:
            return await reject(query, action, arg)
        await query.answer()
        return None

    @router.callback_query(F.data.startswith("flt"))
    async def on_broken_button(query: CallbackQuery) -> None:
        log.warning("неразборчивая кнопка меню: %r", (query.data or "")[:64])
        await query.answer("Неизвестная кнопка, откройте /filters", show_alert=True)

    def find(options: list[Option], arg: str) -> Option | None:
        return next((o for o in options if short_hash(o.value) == arg), None)

    async def reject(query: CallbackQuery, action: str, arg: str) -> None:
        log.warning("отклонена кнопка меню: действие=%r аргумент=%r", action[:16], arg[:16])
        await query.answer("Неизвестная кнопка, откройте /filters", show_alert=True)

    async def stale_button(query: CallbackQuery, message: Message, screen: Screen) -> None:
        await show(message, screen)
        await query.answer("Список обновился, нажмите ещё раз")

    return router
