"""Тесты команд бота через настоящий aiogram Dispatcher с подменённой отправкой в Telegram."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from jobbot.bot.handlers import BotDeps, build_dispatcher, resume_since
from jobbot.config import DEFAULT_CATEGORIES, DEFAULT_EXCLUDE, DEFAULT_INCLUDE, Settings
from jobbot.models import Ad
from jobbot.notifier import Notifier
from jobbot.poller import Poller
from jobbot.storage import Storage, to_iso

OWNER = 111
STRANGER = 222
IT, STUDENTS = DEFAULT_CATEGORIES


class FakeFetcher:
    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None

    async def fetch_page(self, category_url: str, page_no: int = 1) -> str:
        if self.gate is not None:
            await self.gate.wait()
        return "<html></html>"


async def no_sleep(_seconds: float) -> None:
    return None


class Harness:
    def __init__(
        self, bot: Bot, deps: BotDeps, storage: Storage, fetcher: FakeFetcher, calls: list[Any]
    ) -> None:
        self.bot, self.deps, self.storage, self.fetcher, self.calls = (
            bot,
            deps,
            storage,
            fetcher,
            calls,
        )
        self.dp = build_dispatcher(deps)
        self._update_id = 0

    def _next(self) -> int:
        self._update_id += 1
        return self._update_id

    async def say(self, text: str, chat_id: int = OWNER) -> None:
        user = User(id=chat_id, is_bot=False, first_name="u")
        message = Message(
            message_id=self._next(),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=user,
            text=text,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._next(), message=message))

    async def press(self, data: str, chat_id: int = OWNER) -> None:
        user = User(id=chat_id, is_bot=False, first_name="u")
        message = Message(
            message_id=self._next(),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            text="menu",
        )
        query = CallbackQuery(id="1", from_user=user, chat_instance="c", message=message, data=data)
        await self.dp.feed_update(self.bot, Update(update_id=self._next(), callback_query=query))

    @property
    def replies(self) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, SendMessage)]

    def reset(self) -> None:
        self.calls.clear()


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    calls: list[Any] = []

    async def fake_request(bot: Bot, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        calls.append(method)
        if isinstance(method, GetMe):
            return User(id=999, is_bot=True, first_name="bot", username="somon_test_bot")
        if isinstance(method, SendMessage | EditMessageText):
            chat_id = method.chat_id if isinstance(method, SendMessage) else OWNER
            return Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=int(chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError(f"неожиданный вызов Telegram: {method}")

    bot = Bot(token="123456789:" + "A" * 35)
    bot.session.make_request = fake_request  # type: ignore[method-assign]

    storage = await Storage.open(tmp_path / "jobbot.db")
    await storage.seed_defaults(DEFAULT_CATEGORIES, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    settings = Settings(_env_file=None, bot_token="x", owner_chat_id=OWNER)  # noqa: S106
    fetcher = FakeFetcher()
    notifier = Notifier(bot, OWNER, sleep=no_sleep)
    poller = Poller(settings, storage, fetcher, notifier)  # type: ignore[arg-type]
    deps = BotDeps(settings, storage, poller, notifier)
    yield Harness(bot, deps, storage, fetcher, calls)
    await bot.session.close()
    await storage.close()


async def add_ads(storage: Storage, count: int, matched: bool = True) -> None:
    ads = [
        Ad(
            i,
            f"https://somon.tj/adv/{i}_x/",
            f"Python вакансия {i}",
            "it",
            "3 000 c.",
            "Душанбе",
            "Сегодня",
        )
        for i in range(1, count + 1)
    ]
    await storage.add_ads([(ad, matched) for ad in ads], sent=True)


# ---------- доступ ----------


@pytest.mark.parametrize(
    "text", ["/start", "/status", "/check", "/pause", "/include python", "/last"]
)
async def test_stranger_gets_no_reply(h: Harness, text: str) -> None:
    await h.say(text, chat_id=STRANGER)
    assert h.calls == []  # ни ответа, ни вызова Telegram вообще
    assert await h.storage.get_state("paused") is None


async def test_stranger_cannot_press_buttons(h: Harness) -> None:
    await h.press("cat:t:it", chat_id=STRANGER)
    assert h.calls == []
    assert all(c.enabled for c in await h.storage.get_categories())


async def test_stranger_warning_is_logged(h: Harness, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="bot.access"):
        await h.say("/start", chat_id=STRANGER)
    assert "222" in caplog.text


async def test_owner_gets_replies(h: Harness) -> None:
    await h.say("/start")
    await h.say("/help")
    assert len(h.replies) == 2
    assert "/status" in h.replies[0] and "/last" in h.replies[1]


# ---------- слова фильтра ----------


async def test_include_exclude_remove(h: Harness) -> None:
    await h.say("/include  Стажёр Java ")
    assert "стажер java" in await h.storage.get_keywords("include")
    assert "добавлено в include" in h.replies[-1]

    await h.say("/include стажер java")
    assert "уже есть" in h.replies[-1]

    await h.say("/exclude продавец")
    assert await h.storage.get_keywords("exclude") == sorted([*DEFAULT_EXCLUDE, "продавец"])

    await h.say("/remove ПРОДАВЕЦ")
    assert "удалено" in h.replies[-1]
    assert "продавец" not in await h.storage.get_keywords("exclude")

    await h.say("/remove нет такого")
    assert "нет ни в include" in h.replies[-1]


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("/include", "Укажите слово"),
        ("/include a", "короткое"),
        ("/include " + "я" * 41, "длинное"),
        ("/include <b>x</b>", "Недопустимые"),
        ("/exclude drop;table", "Недопустимые"),
        ("/remove", "Укажите слово"),
    ],
)
async def test_invalid_input_gives_clear_error(h: Harness, text: str, fragment: str) -> None:
    before = await h.storage.get_keywords("include")
    await h.say(text)
    assert len(h.replies) == 1
    assert fragment in h.replies[0]
    assert await h.storage.get_keywords("include") == before


async def test_word_limit_per_list(h: Harness) -> None:
    for i in range(100 - len(DEFAULT_INCLUDE)):
        await h.storage.add_keyword("include", f"слово{i}")
    assert await h.storage.count_keywords("include") == 100
    await h.say("/include ещеодно")
    assert "максимум" in h.replies[-1]
    assert await h.storage.count_keywords("include") == 100


async def test_user_text_is_escaped_in_replies(h: Harness) -> None:
    await h.say("/include python")  # обычное слово, уже есть → ответ с эхо
    await h.say("/include <i>")
    assert "<i>" not in h.replies[-1]


async def test_filters_view(h: Harness) -> None:
    await h.say("/filters")
    text = h.replies[0]
    assert "python" in text and "колл-центр" in text
    assert "IT, телеком, компьютеры" in text and "режим all" in text


# ---------- разделы ----------


async def test_categories_buttons_toggle_and_mode(h: Harness) -> None:
    await h.say("/categories")
    markup = next(c for c in h.calls if isinstance(c, SendMessage)).reply_markup
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == ["cat:t:it", "cat:m:it", "cat:t:students", "cat:m:students"]

    await h.press("cat:t:it")
    cats = {c.key: c for c in await h.storage.get_categories()}
    assert not cats["it"].enabled and cats["students"].enabled
    assert any(isinstance(c, EditMessageText) for c in h.calls)

    await h.press("cat:m:students")
    await h.press("cat:t:it")
    cats = {c.key: c for c in await h.storage.get_categories()}
    assert cats["it"].enabled and cats["students"].mode == "keywords"
    await h.press("cat:m:students")
    assert (await h.storage.get_categories())[1].mode == "all"


@pytest.mark.parametrize("data", ["cat:t:nope", "cat:x:it", "cat:t", "cat:t:it:extra"])
async def test_bad_callback_data_is_rejected(h: Harness, data: str) -> None:
    await h.press(data)
    assert all(c.enabled and c.mode == "all" for c in await h.storage.get_categories())


# ---------- /last ----------


async def test_last_default_and_n(h: Harness) -> None:
    await add_ads(h.storage, 12)
    await h.say("/last")
    assert len(h.replies) == 5
    h.reset()
    await h.say("/last 3")
    assert len(h.replies) == 3
    assert "Python вакансия 12" in h.replies[0]  # новые первыми


async def test_last_large_n_becomes_digest(h: Harness) -> None:
    await add_ads(h.storage, 12)
    await h.say("/last 12")
    assert len(h.replies) == 1 and "Найдено 12" in h.replies[0]


@pytest.mark.parametrize("arg", ["0", "21", "-1", "abc", "2.5"])
async def test_last_validation(h: Harness, arg: str) -> None:
    await add_ads(h.storage, 3)
    await h.say(f"/last {arg}")
    assert len(h.replies) == 1 and "❌" in h.replies[0]


async def test_last_when_empty(h: Harness) -> None:
    await h.say("/last")
    assert "нет подходящих" in h.replies[0]


# ---------- пауза ----------


async def test_pause_and_resume_flushes_backlog(h: Harness) -> None:
    await h.say("/pause")
    assert await h.storage.get_state("paused") == "1"
    await h.say("/pause")
    assert "Уже на паузе" in h.replies[-1]

    # за время паузы накопились 2 неотправленные вакансии
    ads = [Ad(i, f"https://somon.tj/adv/{i}_x/", "Python", "it") for i in (1, 2)]
    await h.storage.add_ads([(ad, True) for ad in ads])
    h.reset()
    await h.say("/resume")
    assert await h.storage.get_state("paused") == "0"
    assert "Накопленных вакансий: 2" in h.replies[0]
    assert len(h.replies) == 3  # ответ + 2 карточки
    assert await h.storage.unsent_matched(datetime.now(UTC) - timedelta(days=1)) == []

    await h.say("/resume")
    assert "и так включены" in h.replies[-1]


async def test_resume_with_many_backlog_sends_digest(h: Harness) -> None:
    await h.say("/pause")
    ads = [Ad(i, f"https://somon.tj/adv/{i}_x/", "Python", "it") for i in range(1, 11)]
    await h.storage.add_ads([(ad, True) for ad in ads])
    h.reset()
    await h.say("/resume")
    assert any("Найдено 10" in r for r in h.replies)


def test_resume_since_rules() -> None:
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    assert resume_since(None, now) == now - timedelta(hours=24)
    assert resume_since("2026-10-19T12:00:00Z", now) == datetime(2026, 10, 19, 12, tzinfo=UTC)
    assert resume_since("2026-01-01T00:00:00Z", now) == now - timedelta(days=7)


# ---------- /status и /check ----------


async def test_status(h: Harness) -> None:
    await h.say("/status")
    text = h.replies[0]
    assert "включены" in text and "ещё не было" in text and "Объявлений в базе: 0" in text
    assert "Последняя ошибка: нет" in text

    await h.storage.set_state("paused", "1")
    await h.storage.set_state("last_error", f"{to_iso()} it: <HTTP 500>")
    await h.storage.set_state("blocked_until", to_iso(datetime.now(UTC) + timedelta(minutes=10)))
    await add_ads(h.storage, 2)
    await h.say("/status")
    text = h.replies[1]
    assert "на паузе" in text and "&lt;HTTP 500&gt;" in text and "ограничил доступ" in text
    assert "Объявлений в базе: 2" in text


async def test_check_runs_poll_and_reports(h: Harness) -> None:
    await h.say("/check")
    assert "Проверяю" in h.replies[0]
    assert "Проверка завершена" in h.replies[1]
    assert "IT, телеком, компьютеры: найдено 0" in h.replies[1]
    assert await h.storage.get_state("last_poll_at")


async def test_check_rate_limited_to_once_a_minute(h: Harness) -> None:
    await h.say("/check")
    h.reset()
    await h.say("/check")
    assert len(h.replies) == 1 and "Слишком часто" in h.replies[0]


async def test_check_while_poll_is_running(h: Harness) -> None:
    h.fetcher.gate = asyncio.Event()
    first = asyncio.create_task(h.say("/check"))
    await asyncio.sleep(0.1)
    assert h.deps.poller.busy

    await h.say("/check")
    assert h.replies[-1] == "Проверка уже идёт."

    h.fetcher.gate.set()
    await first
    assert any("Проверка завершена" in r for r in h.replies)
