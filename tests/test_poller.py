"""Тесты poller: seed первого запуска, дубли, новые объявления, пауза, ошибки, блокировка."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage

from jobbot.config import DEFAULT_CATEGORIES, DEFAULT_EXCLUDE, DEFAULT_INCLUDE, Settings
from jobbot.fetcher import BlockedError, FetchError
from jobbot.notifier import Notifier
from jobbot.poller import STATE_BLOCKED_UNTIL, STATE_LAST_POLL, CycleStats, Poller
from jobbot.storage import Storage

IT, STUDENTS = DEFAULT_CATEGORIES
EMPTY = "<html><body></body></html>"
ALERT_MARKS = ("⚠️", "🚫", "✅", "🤖")


def page(*ads: tuple[int, str]) -> str:
    """HTML страницы раздела в формате, который понимает основной парсер."""
    cards = "".join(
        f'<div data-component="JobCardDesktop"><meta itemprop="title" content="{title}">'
        f'<a itemprop="url" href="/adv/{ad_id}_x/"><span>{title}</span></a></div>'
        for ad_id, title in ads
    )
    return f"<html><body>{cards}</body></html>"


def ads_range(first: int, last: int) -> list[tuple[int, str]]:
    return [(i, f"Вакансия {i}") for i in range(first, last + 1)]


class FakeFetcher:
    """Подмена Fetcher: страницы разделов задаются списком; значение-исключение будет брошено."""

    def __init__(self) -> None:
        self.pages: dict[str, list[str | Exception]] = {}
        self.calls: list[tuple[str, int]] = []
        self.on_call: Any = None

    def set(self, category_url: str, *pages: str | Exception) -> None:
        self.pages[category_url] = list(pages)

    async def fetch_page(self, category_url: str, page_no: int = 1) -> str:
        self.calls.append((category_url, page_no))
        if self.on_call:
            await self.on_call()
        pages = self.pages.get(category_url, [])
        item = pages[page_no - 1] if page_no <= len(pages) else EMPTY
        if isinstance(item, Exception):
            raise item
        return item


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []  # вакансии
        self.alert_texts: list[str] = []  # служебные уведомления (⚠️ 🚫 ✅ 🤖)
        self.fail = False

    async def send_message(self, **kwargs: Any) -> None:
        if self.fail:
            raise TelegramNetworkError(SendMessage(chat_id=1, text="x"), "down")
        if kwargs["text"].startswith(ALERT_MARKS):
            self.alert_texts.append(kwargs["text"])
        else:
            self.sent.append(kwargs)


async def no_sleep(_seconds: float) -> None:
    return None


class Env:
    def __init__(self, storage: Storage, fetcher: FakeFetcher, bot: FakeBot, poller: Poller):
        self.storage, self.fetcher, self.bot, self.poller = storage, fetcher, bot, poller


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"bot_token": "123:abc", "owner_chat_id": 1, "seed_send_last": 3}
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    storage = await Storage.open(tmp_path / "jobbot.db")
    await storage.seed_defaults(DEFAULT_CATEGORIES, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    fetcher, bot = FakeFetcher(), FakeBot()
    notifier = Notifier(bot, 1, sleep=no_sleep)  # type: ignore[arg-type]
    poller = Poller(make_settings(), storage, fetcher, notifier)  # type: ignore[arg-type]
    yield Env(storage, fetcher, bot, poller)
    await storage.close()


def texts(env: Env) -> list[str]:
    return [m["text"] for m in env.bot.sent]


# ---------- первый запуск ----------


async def test_first_run_sends_exactly_seed_send_last(env: Env) -> None:
    env.fetcher.set(IT.url, page(*ads_range(1, 10)))
    env.fetcher.set(STUDENTS.url, EMPTY)
    stats = await env.poller.poll_once()

    assert stats is not None
    assert len(env.bot.sent) == 3  # ровно SEED_SEND_LAST сообщений
    assert "Последние вакансии в разделе «IT, телеком, компьютеры»" in texts(env)[0]
    # присланы самые свежие (большие id): 10, 9, 8
    assert [t.count("Вакансия") for t in texts(env)] == [1, 1, 1]
    assert "Вакансия 10" in texts(env)[0] and "Вакансия 8" in texts(env)[2]
    # остальное сохранено как просмотренное и не будет прислано
    assert await env.storage.count_ads() == 10
    assert await env.storage.unsent_matched(since=_long_ago()) == []
    assert stats.categories[0].found == 10 and stats.categories[0].new == 10


async def test_second_run_with_same_page_sends_nothing(env: Env) -> None:
    env.fetcher.set(IT.url, page(*ads_range(1, 10)))
    await env.poller.poll_once()
    env.bot.sent.clear()

    await env.poller.poll_once()
    assert env.bot.sent == []
    assert await env.storage.count_ads() == 10


async def test_new_ad_sends_exactly_one_message(env: Env) -> None:
    env.fetcher.set(IT.url, page(*ads_range(1, 10)))
    await env.poller.poll_once()
    env.bot.sent.clear()

    env.fetcher.set(IT.url, page((11, "Python разработчик"), *ads_range(1, 10)))
    await env.poller.poll_once()
    assert len(env.bot.sent) == 1
    assert "Python разработчик" in texts(env)[0]
    assert env.bot.sent[0]["reply_markup"].inline_keyboard[0][0].url == "https://somon.tj/adv/11_x/"

    env.bot.sent.clear()
    await env.poller.poll_once()  # тот же набор — тишина
    assert env.bot.sent == []


async def test_seed_is_per_category(env: Env) -> None:
    env.fetcher.set(IT.url, page(*ads_range(1, 5)))
    await env.poller.poll_once()
    env.bot.sent.clear()
    # раздел «students» впервые получил объявления — для него снова seed, а не 5 новых карточек
    env.fetcher.set(STUDENTS.url, page(*ads_range(100, 104)))
    await env.poller.poll_once()
    assert len(env.bot.sent) == 3
    assert "Последние вакансии в разделе «Начало карьеры, студенты»" in texts(env)[0]


# ---------- фильтры ----------


async def test_exclude_and_keywords_mode(env: Env) -> None:
    await env.storage.set_category_mode("it", "keywords")
    env.fetcher.set(IT.url, page((1, "Старт")))
    await env.poller.poll_once()  # seed
    env.bot.sent.clear()

    env.fetcher.set(
        IT.url,
        page(
            (2, "Python разработчик"),
            (3, "Продавец"),
            (4, "Python оператор колл-центра"),
            (1, "Старт"),
        ),
    )
    await env.poller.poll_once()
    assert len(env.bot.sent) == 1 and "Python разработчик" in texts(env)[0]
    # отфильтрованные тоже сохранены (matched=0), чтобы не проверять их снова
    assert await env.storage.count_ads() == 4
    env.bot.sent.clear()
    await env.poller.poll_once()
    assert env.bot.sent == []


# ---------- многостраничность ----------


async def test_next_page_only_when_all_new(env: Env) -> None:
    env.fetcher.set(IT.url, page(*ads_range(1, 3)))
    await env.poller.poll_once()
    env.fetcher.calls.clear()

    # на странице есть знакомое объявление → страницу 2 не берём
    env.fetcher.set(IT.url, page((4, "Новая"), (1, "Старая")), page((99, "Не должна загрузиться")))
    await env.poller.poll_once()
    assert (IT.url, 2) not in env.fetcher.calls

    # всё на странице новое → берём 2, и 3, но не дальше MAX_PAGES (3)
    env.fetcher.calls.clear()
    env.fetcher.set(
        IT.url,
        page((10, "A"), (11, "B")),
        page((12, "C"), (13, "D")),
        page((14, "E")),
        page((15, "F")),
    )
    await env.poller.poll_once()
    assert [c for c in env.fetcher.calls if c[0] == IT.url] == [
        (IT.url, 1),
        (IT.url, 2),
        (IT.url, 3),
    ]
    assert await env.storage.count_ads() == 3 + 1 + 5


# ---------- пауза и досылка ----------


async def test_pause_accumulates_and_resume_flushes(env: Env) -> None:
    env.fetcher.set(IT.url, page(*ads_range(1, 3)))
    await env.poller.poll_once()
    env.bot.sent.clear()

    await env.storage.set_state("paused", "1")
    env.fetcher.set(IT.url, page((4, "Первая"), (5, "Вторая"), *ads_range(1, 3)))
    await env.poller.poll_once()
    assert env.bot.sent == []  # опрос идёт, отправки нет
    assert await env.storage.count_ads() == 5

    await env.storage.set_state("paused", "0")
    assert await env.poller.flush_unsent() == 2
    assert len(env.bot.sent) == 2
    assert await env.poller.flush_unsent() == 0  # повторно ничего


async def test_failed_send_is_retried_without_duplicates(env: Env) -> None:
    env.fetcher.set(IT.url, page(*ads_range(1, 3)))
    await env.poller.poll_once()
    env.bot.sent.clear()

    env.bot.fail = True
    env.fetcher.set(IT.url, page((4, "Новая"), *ads_range(1, 3)))
    await env.poller.poll_once()
    assert env.bot.sent == []
    assert [a.ad_id for a in await env.storage.unsent_matched(_long_ago())] == [4]

    env.bot.fail = False
    await env.poller.poll_once()  # следующий цикл доотправляет
    assert len(env.bot.sent) == 1
    env.bot.sent.clear()
    await env.poller.poll_once()
    assert env.bot.sent == []


async def test_startup_flush_sends_unsent_from_before_restart(env: Env) -> None:
    # объявление сохранено, но процесс упал до отправки
    from jobbot.models import Ad

    ad = Ad(7, "https://somon.tj/adv/7_x/", "Python", "it")
    await env.storage.add_ads([(ad, True)])
    assert await env.poller.flush_unsent() == 1
    assert len(env.bot.sent) == 1
    assert await env.poller.flush_unsent() == 0


async def test_more_than_threshold_new_ads_is_one_digest(env: Env) -> None:
    env.fetcher.set(IT.url, page(*ads_range(1, 3)))
    await env.poller.poll_once()
    env.bot.sent.clear()

    env.fetcher.set(IT.url, page(*ads_range(100, 108), *ads_range(1, 3)))  # 9 новых > 8
    await env.poller.poll_once()
    assert len(env.bot.sent) == 1
    assert "Найдено 9 новых вакансий" in texts(env)[0]


# ---------- разделы, ошибки, блокировка ----------


async def test_disabled_category_is_not_polled(env: Env) -> None:
    await env.storage.set_category_enabled("students", False)
    env.fetcher.set(IT.url, page(*ads_range(1, 2)))
    await env.poller.poll_once()
    assert {url for url, _ in env.fetcher.calls} == {IT.url}


async def test_error_in_one_category_does_not_stop_others(env: Env) -> None:
    env.fetcher.set(IT.url, FetchError("HTTP 500"))
    env.fetcher.set(STUDENTS.url, page(*ads_range(1, 4)))
    stats = await env.poller.poll_once()
    assert stats is not None
    assert stats.categories[0].error and "500" in stats.categories[0].error
    assert stats.categories[1].found == 4
    assert len(env.bot.sent) == 3  # seed второго раздела
    assert "it: HTTP 500" in (await env.storage.get_state("last_error") or "")
    assert await env.storage.get_state(STATE_LAST_POLL)


async def test_blocked_pauses_polling(env: Env) -> None:
    env.fetcher.set(IT.url, BlockedError(429, IT.url))
    await env.poller.poll_once()
    assert await env.storage.get_state(STATE_BLOCKED_UNTIL)
    assert {url for url, _ in env.fetcher.calls} == {IT.url}  # второй раздел не трогали

    env.fetcher.calls.clear()
    stats = await env.poller.poll_once()
    assert stats is not None and stats.skipped == "blocked"
    assert env.fetcher.calls == []  # во время паузы запросов нет


async def test_parallel_poll_is_refused(env: Env) -> None:
    gate = asyncio.Event()

    async def wait_gate() -> None:
        await gate.wait()

    env.fetcher.on_call = wait_gate
    first = asyncio.create_task(env.poller.poll_once())
    await asyncio.sleep(0.05)
    assert env.poller.busy
    assert await env.poller.poll_once() is None  # «Проверка уже идёт»

    gate.set()
    assert await first is not None
    assert not env.poller.busy


async def test_run_forever_stops_on_event_and_survives_errors(env: Env) -> None:
    stop = asyncio.Event()
    calls = 0

    async def boom() -> CycleStats | None:
        nonlocal calls
        calls += 1
        stop.set()
        raise RuntimeError("неожиданная ошибка")

    env.poller.poll_once = boom  # type: ignore[method-assign]
    await asyncio.wait_for(env.poller.run_forever(stop), timeout=5)
    assert calls == 1  # ошибка не уронила цикл, остановка по событию мгновенная


async def test_category_exception_is_isolated(env: Env) -> None:
    async def boom() -> None:
        raise RuntimeError("неожиданная ошибка")

    env.fetcher.on_call = boom
    stats = await env.poller.poll_once()
    assert stats is not None
    assert all(c.error for c in stats.categories)  # оба раздела обработаны, цикл жив
    assert await env.storage.get_state(STATE_LAST_POLL)


def test_next_delay_has_jitter_and_floor() -> None:
    import random

    rng = random.Random(1)  # noqa: S311
    poller = Poller(make_settings(), None, None, None, rng=rng)  # type: ignore[arg-type]
    delays = [poller.next_delay() for _ in range(200)]
    assert all(240 <= d <= 360 for d in delays)
    assert max(delays) - min(delays) > 30  # джиттер действительно есть

    tight = Poller(  # type: ignore[arg-type]
        make_settings(poll_interval_sec=180, poll_jitter_sec=60), None, None, None
    )
    assert all(tight.next_delay() >= 180 for _ in range(200))


def _long_ago():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(days=3650)


# ---------- самодиагностика и уведомления об ошибках (этап 6) ----------


async def only_it(env: Env) -> None:
    await env.storage.set_category_enabled("students", False)


def alerts(env: Env) -> list[str]:
    return env.bot.alert_texts


async def test_three_empty_pages_give_one_warning(env: Env) -> None:
    await only_it(env)
    env.fetcher.set(IT.url, EMPTY)
    for _ in range(2):
        await env.poller.poll_once()
    assert alerts(env) == []  # пока только 2 пустых цикла

    await env.poller.poll_once()
    assert len(alerts(env)) == 1
    assert "не нашёл объявлений 3 раза подряд" in alerts(env)[0]
    assert "IT, телеком, компьютеры" in alerts(env)[0]
    assert "вёрстку" in alerts(env)[0]

    await env.poller.poll_once()  # 4-й и 5-й цикл — повтор не раньше чем через 6 часов
    await env.poller.poll_once()
    assert len(alerts(env)) == 1


async def test_empty_streak_resets_when_ads_return(env: Env) -> None:
    await only_it(env)
    env.fetcher.set(IT.url, EMPTY)
    await env.poller.poll_once()
    await env.poller.poll_once()
    env.fetcher.set(IT.url, page(*ads_range(1, 3)))
    await env.poller.poll_once()
    env.fetcher.set(IT.url, EMPTY)
    await env.poller.poll_once()
    await env.poller.poll_once()
    assert alerts(env) == []  # серия началась заново, до трёх не дошла


async def test_empty_alert_repeats_after_six_hours(env: Env) -> None:
    from datetime import UTC, datetime, timedelta

    await only_it(env)
    env.fetcher.set(IT.url, EMPTY)
    for _ in range(3):
        await env.poller.poll_once()
    assert len(alerts(env)) == 1
    old = (datetime.now(UTC) - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await env.storage.set_state("last_alert_at:empty:it", old)
    await env.poller.poll_once()
    assert len(alerts(env)) == 2


async def test_429_sends_single_notification(env: Env) -> None:
    await only_it(env)
    env.fetcher.set(IT.url, BlockedError(429, IT.url))
    await env.poller.poll_once()
    assert len(alerts(env)) == 1
    assert "429" in alerts(env)[0] and "30 минут" in alerts(env)[0]

    await env.poller.poll_once()  # во время паузы — тишина и без запросов
    await env.storage.set_state("blocked_until", "2000-01-01T00:00:00Z")  # пауза истекла
    await env.poller.poll_once()  # снова 429: тот же эпизод, второго уведомления нет
    assert len(alerts(env)) == 1


async def test_block_episode_ends_after_success(env: Env) -> None:
    await only_it(env)
    env.fetcher.set(IT.url, BlockedError(403, IT.url))
    await env.poller.poll_once()
    await env.storage.set_state("blocked_until", "2000-01-01T00:00:00Z")
    env.fetcher.set(IT.url, page(*ads_range(1, 3)))
    await env.poller.poll_once()  # сайт снова отвечает
    assert await env.storage.get_state("block_alerted") is None

    env.fetcher.set(IT.url, BlockedError(429, IT.url))
    await env.poller.poll_once()  # новая блокировка — новое уведомление
    assert len([a for a in alerts(env) if a.startswith("🚫")]) == 2


async def test_five_network_errors_one_alert_then_recovery(env: Env) -> None:
    await only_it(env)
    env.fetcher.set(IT.url, FetchError("таймаут"))
    for _ in range(4):
        await env.poller.poll_once()
    assert alerts(env) == []

    await env.poller.poll_once()  # пятая подряд
    assert len(alerts(env)) == 1 and "5 ошибок подряд" in alerts(env)[0]
    for _ in range(3):
        await env.poller.poll_once()
    assert len(alerts(env)) == 1  # без спама

    env.fetcher.set(IT.url, page(*ads_range(1, 3)))
    await env.poller.poll_once()
    assert [a for a in alerts(env) if a.startswith("✅")] == ["✅ Связь с сайтом восстановлена."]
    await env.poller.poll_once()
    assert len([a for a in alerts(env) if a.startswith("✅")]) == 1


async def test_few_errors_then_success_is_silent(env: Env) -> None:
    await only_it(env)
    env.fetcher.set(IT.url, FetchError("таймаут"))
    for _ in range(3):
        await env.poller.poll_once()
    env.fetcher.set(IT.url, page(*ads_range(1, 3)))
    await env.poller.poll_once()
    assert not [a for a in alerts(env) if a.startswith("✅")]


async def test_robots_disallowed_alert_once(env: Env) -> None:
    from jobbot.fetcher import RobotsDisallowedError

    await only_it(env)
    env.fetcher.set(IT.url, RobotsDisallowedError("robots.txt запрещает"))
    await env.poller.poll_once()
    await env.poller.poll_once()
    assert len(alerts(env)) == 1 and "robots.txt" in alerts(env)[0]


async def test_alerts_are_delivered_while_paused(env: Env) -> None:
    await only_it(env)
    await env.storage.set_state("paused", "1")
    env.fetcher.set(IT.url, BlockedError(429, IT.url))
    await env.poller.poll_once()
    assert len(alerts(env)) == 1


async def test_failed_alert_delivery_does_not_break_polling(env: Env) -> None:
    await only_it(env)
    env.bot.fail = True
    env.fetcher.set(IT.url, BlockedError(429, IT.url))
    stats = await env.poller.poll_once()
    assert stats is not None
    assert await env.storage.get_state("blocked_until")
    assert await env.storage.get_state("block_alerted") is None  # не доставлено → повторим позже
