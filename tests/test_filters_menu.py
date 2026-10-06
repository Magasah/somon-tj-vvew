"""Меню фильтров (шаг 9.6): настоящий aiogram Dispatcher, Telegram подменён на уровне сессии."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    GetMe,
    SendMessage,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from jobbot.bot.filters_menu import FILTERS_BUTTON, FilterCB, short_hash
from jobbot.bot.handlers import BotDeps, build_dispatcher
from jobbot.config import DEFAULT_CATEGORIES, DEFAULT_EXCLUDE, DEFAULT_INCLUDE, Settings
from jobbot.filters import FilterProfile, SalaryFilter
from jobbot.models import Ad
from jobbot.notifier import Notifier
from jobbot.poller import Poller
from jobbot.storage import Storage

OWNER = 111
STRANGER = 222


class FakeFetcher:
    async def fetch_page(self, category_url: str, page_no: int = 1) -> str:
        return "<html></html>"


async def no_sleep(_seconds: float) -> None:
    return None


class Telegram:
    """Подмена Telegram: хранит сообщения бота, ответы на нажатия, удаления."""

    def __init__(self) -> None:
        self.messages: dict[int, tuple[str, Any]] = {}  # id → (текст, клавиатура)
        self.sent_ids: list[int] = []
        self.answers: list[tuple[str | None, bool]] = []
        self.deleted: list[int] = []
        self.edits = 0
        self._next_id = 1000

    async def __call__(self, bot: Bot, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, GetMe):
            return User(id=999, is_bot=True, first_name="bot", username="test_bot")
        if isinstance(method, SendMessage):
            self._next_id += 1
            self.messages[self._next_id] = (method.text, method.reply_markup)
            self.sent_ids.append(self._next_id)
            return self._message(self._next_id, method.text)
        if isinstance(method, EditMessageText):
            old = self.messages.get(method.message_id)
            if old == (method.text, method.reply_markup):
                raise TelegramBadRequest(method, "Bad Request: message is not modified")
            self.messages[method.message_id] = (method.text, method.reply_markup)
            self.edits += 1
            return self._message(method.message_id, method.text)
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append((method.text, bool(method.show_alert)))
            return True
        if isinstance(method, DeleteMessage):
            self.deleted.append(method.message_id)
            return True
        raise AssertionError(f"неожиданный вызов Telegram: {method}")

    @staticmethod
    def _message(message_id: int, text: str) -> Message:
        chat = Chat(id=OWNER, type="private")
        return Message(message_id=message_id, date=datetime.now(UTC), chat=chat, text=text)


class Menu:
    def __init__(self, dp: Any, bot: Bot, tg: Telegram, storage: Storage) -> None:
        self.dp, self.bot, self.tg, self.storage = dp, bot, tg, storage
        self._update_id = 0
        self.menu_id: int | None = None

    def _uid(self) -> int:
        self._update_id += 1
        return self._update_id

    async def say(self, text: str, chat_id: int = OWNER) -> None:
        message = Message(
            message_id=self._uid() + 50000,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="u"),
            text=text,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._uid(), message=message))

    async def open(self) -> None:
        await self.say("/filters")
        self.menu_id = self.tg.sent_ids[-1]

    @property
    def text(self) -> str:
        assert self.menu_id is not None
        return self.tg.messages[self.menu_id][0]

    @property
    def keyboard(self) -> InlineKeyboardMarkup:
        assert self.menu_id is not None
        markup = self.tg.messages[self.menu_id][1]
        assert isinstance(markup, InlineKeyboardMarkup)
        return markup

    def buttons(self) -> list[str]:
        return [b.text for row in self.keyboard.inline_keyboard for b in row]

    def data_of(self, label: str) -> str:
        matches = [
            b.callback_data for row in self.keyboard.inline_keyboard for b in row if label in b.text
        ]
        assert matches, f"нет кнопки «{label}» среди {self.buttons()}"
        return matches[0]  # type: ignore[return-value]

    async def press(self, label: str) -> None:
        await self.press_data(self.data_of(label))

    async def press_data(
        self, data: str, *, chat_id: int = OWNER, age: timedelta = timedelta(0)
    ) -> None:
        assert self.menu_id is not None
        message = Message(
            message_id=self.menu_id,
            date=datetime.now(UTC) - age,
            chat=Chat(id=chat_id, type="private"),
            text=self.text,
        )
        query = CallbackQuery(
            id=str(self._uid()),
            from_user=User(id=chat_id, is_bot=False, first_name="u"),
            chat_instance="c",
            message=message,
            data=data,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._uid(), callback_query=query))

    @property
    def last_answer(self) -> tuple[str | None, bool]:
        return self.tg.answers[-1]

    async def profile(self) -> FilterProfile:
        return await self.storage.get_filter_profile()


@pytest.fixture
async def menu(tmp_path: Path) -> AsyncIterator[Menu]:
    tg = Telegram()
    bot = Bot(token="123456789:" + "A" * 35)
    bot.session.make_request = tg  # type: ignore[method-assign]
    storage = await Storage.open(tmp_path / "jobbot.db")
    await storage.seed_defaults(DEFAULT_CATEGORIES, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    settings = Settings(_env_file=None, bot_token="x", owner_chat_id=OWNER)  # noqa: S106
    notifier = Notifier(bot, OWNER, sleep=no_sleep)
    poller = Poller(settings, storage, FakeFetcher(), notifier)  # type: ignore[arg-type]
    dp = build_dispatcher(BotDeps(settings, storage, poller, notifier))
    yield Menu(dp, bot, tg, storage)
    await bot.session.close()
    await storage.close()


# ---------- вход в меню ----------


async def test_filters_command_opens_main_menu(menu: Menu) -> None:
    await menu.open()
    assert "⚙️ <b>Ваши фильтры</b>" in menu.text
    for line in ("🗂 Категории", "📍 Город: Все", "💰 Зарплата: Любая", "🕒 График: Любой"):
        assert line in menu.text
    assert "Подходит за последние 7 дней: 0 вакансий" in menu.text
    assert menu.buttons() == [
        "🗂 Категории", "📍 Город", "💰 Зарплата", "🕒 График", "🎓 Стаж",
        "🔤 Ключевые слова", "🔍 Проверить фильтр", "♻️ Сбросить", "✅ Готово",
    ]  # fmt: skip


async def test_start_shows_persistent_filters_button(menu: Menu) -> None:
    await menu.say("/start")
    markup = menu.tg.messages[menu.tg.sent_ids[-1]][1]
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert markup.keyboard[0][0].text == FILTERS_BUTTON
    await menu.say(FILTERS_BUTTON)
    assert "Ваши фильтры" in menu.tg.messages[menu.tg.sent_ids[-1]][0]


async def test_menu_is_edited_in_place(menu: Menu) -> None:
    await menu.open()
    sent_before = len(menu.tg.sent_ids)
    for label in ("📍 Город", "⬅️ Назад", "💰 Зарплата", "⬅️ Назад", "🎓 Стаж"):
        await menu.press(label)
    assert len(menu.tg.sent_ids) == sent_before  # новых сообщений нет
    assert all(answer == (None, False) for answer in menu.tg.answers)  # на каждое нажатие ответ


# ---------- город ----------


async def test_city_toggle_and_all_cities(menu: Menu) -> None:
    await menu.open()
    await menu.press("📍 Город")
    # справочник пуст → стартовые города
    for city in ("Душанбе", "Худжанд", "Кулоб", "Бохтар", "Восе", "Истаравшан"):
        assert any(city in b for b in menu.buttons())
    await menu.press("Худжанд")
    assert (await menu.profile()).cities == ["худжанд"]
    assert "✅ Худжанд" in menu.buttons()
    await menu.press("Душанбе")
    assert (await menu.profile()).cities == ["худжанд", "душанбе"]
    await menu.press("Худжанд")  # повторное нажатие снимает
    assert (await menu.profile()).cities == ["душанбе"]
    await menu.press("Все города")
    assert (await menu.profile()).cities == []
    await menu.press("Все города")  # ничего не изменилось — «message is not modified» не ломает
    assert menu.last_answer == (None, False)


async def test_city_buttons_from_site_values(menu: Menu) -> None:
    for name, times in (("Вахдат", 5), ("Турсунзаде", 3)):
        for _ in range(times):
            await menu.storage.record_attr_values("city", [name])
    await menu.open()
    await menu.press("📍 Город")
    assert menu.buttons()[1:3] == ["▫️ Вахдат", "▫️ Турсунзаде"]  # по частоте, после «Все города»
    assert any("Душанбе" in b for b in menu.buttons())  # мало городов → добавлены стартовые


async def test_city_callback_carries_hash_not_text(menu: Menu) -> None:
    await menu.open()
    await menu.press("📍 Город")
    data = menu.data_of("Душанбе")
    assert "Душанбе" not in data and short_hash("душанбе") in data
    assert len(data.encode()) <= 64


# ---------- зарплата ----------


async def test_salary_manual_input(menu: Menu) -> None:
    await menu.open()
    await menu.press("💰 Зарплата")
    await menu.press("Ввести вручную")
    assert "Введите зарплату" in menu.text
    await menu.say("2000-5000")
    profile = await menu.profile()
    assert (profile.salary.min, profile.salary.max) == (2000, 5000)
    assert "Сейчас: 2 000–5 000 c." in menu.text  # меню вернулось на экран зарплаты
    assert menu.tg.deleted  # введённый текст убран из чата
    await menu.say("3000")  # ввод закончен — обычный текст больше не меняет фильтр
    assert (await menu.profile()).salary.min == 2000


async def test_salary_invalid_input_then_retry(menu: Menu) -> None:
    await menu.open()
    await menu.press("💰 Зарплата")
    await menu.press("Ввести вручную")
    await menu.say("много денег")
    assert "❌ Не понял формат" in menu.text and "2000-5000" in menu.text
    await menu.say("5000-2000")
    assert "«От» больше" in menu.text
    assert (await menu.profile()).salary == SalaryFilter()
    await menu.say("от 3000")
    assert (await menu.profile()).salary.min == 3000


async def test_salary_input_cancel(menu: Menu) -> None:
    await menu.open()
    await menu.press("💰 Зарплата")
    await menu.press("Ввести вручную")
    await menu.say("/cancel")
    assert "Сейчас: Любая" in menu.text
    await menu.say("2000")
    assert (await menu.profile()).salary == SalaryFilter()
    await menu.press("Ввести вручную")
    await menu.press("Отмена")
    assert "Сейчас: Любая" in menu.text


async def test_salary_presets_and_negotiable(menu: Menu) -> None:
    await menu.open()
    await menu.press("💰 Зарплата")
    await menu.press("от 3 000")
    assert (await menu.profile()).salary.min == 3000
    assert "✅ от 3 000" in menu.buttons()
    await menu.press("Договорная")
    assert (await menu.profile()).salary.include_negotiable is False
    assert "«Договорная»: скрывать ▫️" in menu.buttons()
    await menu.press("Любая")
    assert (await menu.profile()).salary == SalaryFilter(include_negotiable=False)


# ---------- график, стаж, «если не указано» ----------


async def test_schedule_values_and_unknown_toggle(menu: Menu) -> None:
    await menu.storage.record_attr_values("schedule", ["Полный день", "Удаленно", "Полный день"])
    await menu.open()
    await menu.press("🕒 График")
    assert "Значения берутся с сайта" in menu.text
    await menu.press("Удаленно")
    assert (await menu.profile()).schedules == ["удаленно"]
    await menu.press("Если не указано")
    assert (await menu.profile()).include_unknown_attrs is False
    await menu.press("Любой")
    assert (await menu.profile()).schedules == []


async def test_experience_site_any_is_labelled_differently(menu: Menu) -> None:
    await menu.storage.record_attr_values("experience", ["Любой", "Без опыта"])
    await menu.open()
    await menu.press("🎓 Стаж")
    assert "▫️ Любой стаж (так на сайте)" in menu.buttons()
    await menu.press("Любой стаж")
    assert (await menu.profile()).experience == ["любой"]
    assert "✅ Любой" not in menu.buttons()[0]  # кнопка «Любой» (= без фильтра) не отмечена


async def test_values_screen_without_known_values(menu: Menu) -> None:
    await menu.open()
    await menu.press("🕒 График")
    assert "появятся после первых вакансий" in menu.text


# ---------- категории ----------


async def test_cannot_remove_last_category(menu: Menu) -> None:
    await menu.open()
    await menu.press("🗂 Категории")
    await menu.press("Начало карьеры")
    assert (await menu.profile()).categories == ["it"]
    await menu.press("IT, телеком")
    assert menu.last_answer == ("Нужна хотя бы одна категория", True)
    assert (await menu.profile()).categories == ["it"]


async def test_only_visible_categories_shown(menu: Menu) -> None:
    await menu.open()
    await menu.press("🗂 Категории")
    assert menu.buttons() == [
        "✅ IT, телеком, компьютеры",
        "✅ Начало карьеры, студенты",
        "⬅️ Назад",
    ]


# ---------- ключевые слова ----------


async def test_keywords_add_and_delete(menu: Menu) -> None:
    await menu.open()
    await menu.press("🔤 Ключевые слова")
    await menu.press("Добавить нужное")
    await menu.say("Java")
    assert "java" in await menu.storage.get_keywords("include")
    assert "java" in menu.text
    await menu.press("Добавить стоп-слово")
    await menu.say("<b>")
    assert "Недопустимые символы" in menu.text  # прежняя валидация
    await menu.say("продавец")
    assert "продавец" in await menu.storage.get_keywords("exclude")
    await menu.press("Удалить слово")
    await menu.press("➖ продавец")
    assert "продавец" not in await menu.storage.get_keywords("exclude")


# ---------- проверка, сброс, готово ----------


async def add_ads(storage: Storage, count: int) -> None:
    ads = [
        Ad(i, f"https://somon.tj/adv/{i}_x/", f"Python {i}", "it", "3 000 c.", "Душанбе")
        for i in range(1, count + 1)
    ]
    await storage.add_ads([(ad, True) for ad in ads], skip_sending=True)


async def test_check_filter_and_send_all(menu: Menu) -> None:
    await add_ads(menu.storage, 7)
    await menu.open()
    assert "Подходит за последние 7 дней: 7 вакансий" in menu.text
    await menu.press("Проверить фильтр")
    assert "Подходит за 7 дней: 7" in menu.text
    assert menu.text.count("• ") == 5  # не больше 5 в списке
    sent_before = len(menu.tg.sent_ids)
    await menu.press("Прислать все")
    assert len(menu.tg.sent_ids) == sent_before + 1  # одним дайджестом
    assert "Найдено 7" in menu.tg.messages[menu.tg.sent_ids[-1]][0]


async def test_count_follows_filters(menu: Menu) -> None:
    await add_ads(menu.storage, 3)
    await menu.storage.update_profile(lambda p: setattr(p, "cities", ["худжанд"]))
    await menu.open()
    assert "Подходит за последние 7 дней: 0 вакансий" in menu.text


async def test_reset_with_confirmation(menu: Menu) -> None:
    await menu.storage.update_profile(lambda p: setattr(p, "cities", ["худжанд"]))
    await menu.open()
    await menu.press("Сбросить")
    assert "Сбросить фильтры?" in menu.text
    await menu.press("Отмена")
    assert (await menu.profile()).cities == ["худжанд"]
    await menu.press("Сбросить")
    await menu.press("Да, сбросить")
    assert await menu.profile() == FilterProfile()
    assert len(await menu.storage.get_keywords("include")) == len(DEFAULT_INCLUDE)


async def test_done_shows_summary(menu: Menu) -> None:
    await menu.open()
    await menu.press("Готово")
    assert menu.text.startswith("✅ <b>Фильтры сохранены</b>")
    assert menu.tg.messages[menu.menu_id][1] is None  # type: ignore[index]


# ---------- безопасность и устойчивость ----------


@pytest.mark.parametrize(
    "data",
    [
        FilterCB(a="zz").pack(),  # неизвестное действие
        FilterCB(a="ct", h="nope").pack(),  # несуществующая категория
        FilterCB(a="ct", h="sales").pack(),  # есть в каталоге, но не видима
        FilterCB(a="sp", h="99").pack(),  # нет такого пресета
        FilterCB(a="unk", h="xxx").pack(),
        "flt",  # неразборчивые данные
        "flt:a:b:c:d",
    ],
)
async def test_forged_callbacks_are_rejected(menu: Menu, data: str) -> None:
    await menu.open()
    before = await menu.profile()
    await menu.press_data(data)
    assert menu.last_answer[1] is True  # всплывашка с отказом
    assert await menu.profile() == before


async def test_unknown_value_hash_refreshes_screen(menu: Menu) -> None:
    await menu.open()
    await menu.press("📍 Город")
    await menu.press_data(FilterCB(a="cy", h="deadbeef").pack())
    assert menu.last_answer == ("Список обновился, нажмите ещё раз", False)
    assert (await menu.profile()).cities == []


async def test_stranger_cannot_use_menu(menu: Menu) -> None:
    await menu.open()
    answers = len(menu.tg.answers)
    await menu.press_data(menu.data_of("📍 Город"), chat_id=STRANGER)
    await menu.say("/filters", chat_id=STRANGER)
    assert len(menu.tg.answers) == answers and len(menu.tg.sent_ids) == 1


async def test_stale_menu(menu: Menu) -> None:
    await menu.open()
    await menu.press_data(menu.data_of("📍 Город"), age=timedelta(hours=25))
    assert menu.last_answer == ("Меню устарело, откройте /filters", True)


async def test_fast_double_presses_keep_all_changes(menu: Menu) -> None:
    await menu.open()
    await menu.press("📍 Город")
    first, second = menu.data_of("Душанбе"), menu.data_of("Худжанд")
    await asyncio.gather(menu.press_data(first), menu.press_data(second))
    assert sorted((await menu.profile()).cities) == ["душанбе", "худжанд"]


async def test_old_commands_still_work(menu: Menu) -> None:
    await menu.say("/include rust")
    await menu.say("/exclude продажи")
    await menu.say("/status")
    assert "rust" in await menu.storage.get_keywords("include")
    assert "продажи" in await menu.storage.get_keywords("exclude")
    assert "Статус" in menu.tg.messages[menu.tg.sent_ids[-1]][0]
