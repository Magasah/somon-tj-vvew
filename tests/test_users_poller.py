"""v1.2: вакансии для друзей — у каждого свои фильтры, отдельная доставка, без дублей."""

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from jobbot.catalog import CATALOG_BY_KEY
from jobbot.config import DEFAULT_EXCLUDE, DEFAULT_INCLUDE, Settings
from jobbot.fetcher import Fetcher
from jobbot.filters import FilterProfile
from jobbot.notifier import Notifier
from jobbot.poller import Poller
from jobbot.storage import Storage

OWNER = 1
FRIEND = 500
IT_URL = CATALOG_BY_KEY["it"].url
STUDENTS_URL = CATALOG_BY_KEY["students"].url
REST_URL = CATALOG_BY_KEY["restaurants"].url
AD_URL_RE = re.compile(r"https://somon\.tj/adv/(\d+)_x/")
EMPTY = "<html><body></body></html>"


def page(*ads: tuple[int, str]) -> str:
    cards = "".join(
        f'<div data-component="JobCardDesktop"><meta itemprop="title" content="{title}">'
        f"<p>Сегодня<i></i>Душанбе</p>"
        f'<a itemprop="url" href="/adv/{ad_id}_x/"><span>{title}</span></a></div>'
        for ad_id, title in ads
    )
    return f"<html><body>{cards}</body></html>"


def detail(experience: str) -> str:
    return (
        '<section data-component="AdvertFeaturesApp"><dl>'
        f"<div><dt>Стаж:</dt><dd>{experience}</dd></div>"
        "<div><dt>График:</dt><dd>Полный день</dd></div></dl></section>"
    )


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.forbidden: set[int] = set()

    async def send_message(self, **kwargs: Any) -> None:
        if kwargs["chat_id"] in self.forbidden:
            raise TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "bot was blocked")
        self.sent.append((kwargs["chat_id"], kwargs["text"]))

    def to(self, chat_id: int) -> list[str]:
        return [t for c, t in self.sent if c == chat_id and t.startswith(("💼", "📌", "🔔"))]

    def alerts(self) -> list[str]:
        return [t for c, t in self.sent if c == OWNER and t.startswith("🚫")]


async def no_sleep(_seconds: float) -> None:
    return None


class Env:
    def __init__(self, storage: Storage, poller: Poller, bot: FakeBot, pages: dict) -> None:
        self.storage, self.poller, self.bot, self.pages = storage, poller, bot, pages
        self.details: dict[int, str] = {}
        self.detail_calls: list[int] = []

    async def cycle(self) -> None:
        await self.poller.poll_once()


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    storage = await Storage.open(tmp_path / "jobbot.db")
    settings = Settings(
        _env_file=None,
        bot_token="x",  # noqa: S106
        owner_chat_id=OWNER,
        visible_categories="it,students,restaurants",
    )
    await storage.seed_defaults(settings.categories, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    bot = FakeBot()
    pages: dict[str, str] = {}

    with respx.mock(assert_all_called=False) as router:
        router.get("https://somon.tj/robots.txt").respond(200, text="User-agent: *\n")
        environment: Env

        def list_page(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("page"):
                return httpx.Response(200, text=EMPTY)
            return httpx.Response(200, text=pages.get(str(request.url), EMPTY))

        def ad_page(request: httpx.Request) -> httpx.Response:
            ad_id = int(AD_URL_RE.match(str(request.url)).group(1))  # type: ignore[union-attr]
            environment.detail_calls.append(ad_id)
            return httpx.Response(200, text=environment.details.get(ad_id, detail("Любой")))

        router.get(url__regex=r"https://somon\.tj/vakansii/.*").mock(side_effect=list_page)
        router.get(url__regex=AD_URL_RE.pattern).mock(side_effect=ad_page)
        async with Fetcher(3, sleep=no_sleep) as fetcher:
            notifier = Notifier(bot, OWNER, sleep=no_sleep)  # type: ignore[arg-type]
            poller = Poller(settings, storage, fetcher, notifier)
            environment = Env(storage, poller, bot, pages)
            yield environment
    await storage.close()


async def add_friend(env: Env, *categories: str, chat_id: int = FRIEND, **profile: Any) -> None:
    await env.storage.add_user(
        chat_id, name="Друг", profile=FilterProfile(categories=list(categories), **profile)
    )


async def test_friend_gets_only_his_category_owner_unaffected(env: Env) -> None:
    await add_friend(env, "restaurants")
    env.pages[IT_URL] = page((1, "Python"), (2, "Java"))
    env.pages[REST_URL] = page((10, "Официант"), (11, "Повар"), (12, "Бармен"), (13, "Кассир"))
    await env.cycle()
    # первый запуск: каждому — по 3 последних вакансии его разделов
    assert len(env.bot.to(FRIEND)) == 3
    assert "Рестораторы" in env.bot.to(FRIEND)[0]
    assert not any("Повар" in t or "Бармен" in t for t in env.bot.to(OWNER))

    env.bot.sent.clear()
    env.pages[REST_URL] = page((14, "Официант в кафе"), (10, "Официант"))
    env.pages[IT_URL] = page((3, "Backend"), (1, "Python"))
    await env.cycle()
    assert [t.splitlines()[0] for t in env.bot.to(FRIEND)] == ["💼 <b>Официант в кафе</b>"]
    assert [t.splitlines()[0] for t in env.bot.to(OWNER)] == ["💼 <b>Backend</b>"]

    env.bot.sent.clear()
    await env.cycle()
    assert env.bot.to(OWNER) == [] and env.bot.to(FRIEND) == []  # дублей нет ни у кого


async def test_only_wanted_categories_polled(env: Env) -> None:
    calls: list[str] = []
    original = env.poller._fetcher.fetch_page

    async def spy(url: str, page_no: int = 1) -> str:
        calls.append(url)
        return await original(url, page_no)

    env.poller._fetcher.fetch_page = spy  # type: ignore[method-assign]
    await env.cycle()
    assert REST_URL not in calls  # ни у кого не выбран
    await add_friend(env, "restaurants")
    await env.cycle()
    assert REST_URL in calls


async def test_friend_city_filter(env: Env) -> None:
    await add_friend(env, "restaurants", cities=["худжанд"])
    env.pages[REST_URL] = page((10, "Официант"))
    await env.cycle()
    assert env.bot.to(FRIEND) == []  # город карточек — Душанбе


async def test_paused_friend_accumulates_and_flushes(env: Env) -> None:
    await add_friend(env, "restaurants")
    env.pages[REST_URL] = page((10, "Официант"))
    await env.cycle()
    env.bot.sent.clear()
    await env.storage.set_user_status(FRIEND, "paused")
    env.pages[REST_URL] = page((11, "Повар"), (12, "Бармен"), (10, "Официант"))
    await env.cycle()
    assert env.bot.to(FRIEND) == []
    await env.storage.set_user_status(FRIEND, "active")
    assert await env.poller.flush_friend(FRIEND) == 2
    assert len(env.bot.to(FRIEND)) == 2
    assert await env.poller.flush_friend(FRIEND) == 0


async def test_details_shared_between_recipients(env: Env) -> None:
    await add_friend(env, "it", experience=["без опыта"])
    env.pages[IT_URL] = page((1, "Старт"))
    await env.cycle()
    env.bot.sent.clear()
    env.details[2] = detail("Без опыта")
    env.details[3] = detail("От 3 лет")
    env.pages[IT_URL] = page((2, "Junior"), (3, "Senior"), (1, "Старт"))
    await env.cycle()
    assert sorted(env.detail_calls) == [2, 3]  # страницы грузятся один раз на всех
    # владельцу график/стаж не нужны — обе сразу; другу — только «Без опыта»
    assert len(env.bot.to(OWNER)) == 2
    assert [t.splitlines()[0] for t in env.bot.to(FRIEND)] == ["💼 <b>Junior</b>"]
    env.bot.sent.clear()
    await env.cycle()
    assert env.bot.to(OWNER) == [] and env.bot.to(FRIEND) == []
    assert sorted(env.detail_calls) == [2, 3]


async def test_owner_rejected_ad_not_marked_by_friend_details(env: Env) -> None:
    # владелец не выбирал раздел; детали, загруженные для друга, не должны «включить» его владельцу
    await env.storage.update_profile(lambda p: setattr(p, "categories", ["students"]))
    await add_friend(env, "it", experience=["без опыта"])
    env.pages[IT_URL] = page((1, "Старт"))
    await env.cycle()
    env.bot.sent.clear()
    env.details[2] = detail("Без опыта")
    env.pages[IT_URL] = page((2, "Junior"), (1, "Старт"))
    await env.cycle()
    assert env.bot.to(OWNER) == [] and len(env.bot.to(FRIEND)) == 1


async def test_friend_who_blocked_bot_is_stopped_once(env: Env) -> None:
    await add_friend(env, "restaurants")
    env.pages[REST_URL] = page((10, "Официант"))
    await env.cycle()
    env.bot.forbidden.add(FRIEND)
    env.pages[REST_URL] = page((11, "Повар"), (10, "Официант"))
    await env.cycle()
    user = await env.storage.get_user(FRIEND)
    assert user is not None and user.status == "blocked"
    assert len(env.bot.alerts()) == 1 and "заблокировал бота" in env.bot.alerts()[0]
    env.pages[REST_URL] = page((12, "Бармен"), (11, "Повар"), (10, "Официант"))
    await env.cycle()
    assert len(env.bot.alerts()) == 1  # больше не пытаемся и не спамим


async def test_deleted_friend_gets_nothing(env: Env) -> None:
    await add_friend(env, "restaurants")
    env.pages[REST_URL] = page((10, "Официант"))
    await env.cycle()
    await env.storage.delete_user(FRIEND)
    env.bot.sent.clear()
    env.pages[REST_URL] = page((11, "Повар"), (10, "Официант"))
    await env.cycle()
    assert env.bot.to(FRIEND) == []
