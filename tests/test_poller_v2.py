"""Опрос v2 (шаг 9.5): разделы по профилю, очередь страниц объявлений, повторы, пометка.

HTTP мокается через respx, а запросы идут через настоящий Fetcher (с подменённым sleep).
"""

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from jobbot.config import DEFAULT_CATEGORIES, DEFAULT_EXCLUDE, DEFAULT_INCLUDE, Settings
from jobbot.fetcher import Fetcher
from jobbot.filters import FilterProfile
from jobbot.notifier import UNCHECKED_MARK, Notifier
from jobbot.poller import Poller
from jobbot.storage import Storage

IT, STUDENTS = DEFAULT_CATEGORIES
ROBOTS_URL = "https://somon.tj/robots.txt"
AD_URL_RE = re.compile(r"https://somon\.tj/adv/(\d+)_x/")
EMPTY = "<html><body></body></html>"


def page(*ads: tuple[int, str] | tuple[int, str, str], city: str = "Душанбе") -> str:
    """Страница раздела; у объявления можно задать свой город третьим элементом."""
    cards = []
    for ad in ads:
        ad_id, title = ad[0], ad[1]
        ad_city = ad[2] if len(ad) > 2 else city  # type: ignore[misc]
        cards.append(
            f'<div data-component="JobCardDesktop"><meta itemprop="title" content="{title}">'
            f"<p>Сегодня<i></i>{ad_city}</p>"
            f'<a itemprop="url" href="/adv/{ad_id}_x/"><span>{title}</span></a></div>'
        )
    return f"<html><body>{''.join(cards)}</body></html>"


def ads_range(first: int, last: int) -> list[tuple[int, str]]:
    return [(i, f"Вакансия {i}") for i in range(first, last + 1)]


def detail(schedule: str = "Полный день", experience: str = "Без опыта") -> str:
    return (
        '<section data-component="AdvertFeaturesApp"><dl>'
        f"<div><dt>Стаж:</dt><dd><a>{experience}</a></dd></div>"
        f"<div><dt>График:</dt><dd>{schedule}</dd></div>"
        "<div><dt>Сфера деятельности компании:</dt><dd>IT</dd></div>"
        "</dl></section>"
    )


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs["text"])


async def no_sleep(_seconds: float) -> None:
    return None


class Env:
    def __init__(self, storage: Storage, poller: Poller, bot: FakeBot, pages: dict) -> None:
        self.storage, self.poller, self.bot, self.pages = storage, poller, bot, pages
        self.details: dict[int, httpx.Response] = {}
        self.detail_calls: list[int] = []

    def vacancies(self) -> list[str]:
        return [t for t in self.bot.sent if t.startswith(("💼", "📌", "🔔"))]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    storage = await Storage.open(tmp_path / "jobbot.db")
    settings = Settings(_env_file=None, bot_token="x", owner_chat_id=1, seed_send_last=3)  # noqa: S106
    await storage.seed_defaults(settings.categories, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    bot = FakeBot()
    pages: dict[str, str] = {IT.url: EMPTY, STUDENTS.url: EMPTY}

    with respx.mock(assert_all_called=False) as router:
        router.get(ROBOTS_URL).respond(200, text="User-agent: *\nDisallow: /api\n")
        environment: Env

        def list_page(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if request.url.params.get("page"):
                return httpx.Response(200, text=EMPTY)
            return httpx.Response(200, text=pages.get(url, EMPTY))

        def ad_page(request: httpx.Request) -> httpx.Response:
            ad_id = int(AD_URL_RE.match(str(request.url)).group(1))  # type: ignore[union-attr]
            environment.detail_calls.append(ad_id)
            return environment.details.get(ad_id, httpx.Response(200, text=detail()))

        router.get(url__regex=r"https://somon\.tj/vakansii/.*").mock(side_effect=list_page)
        router.get(url__regex=AD_URL_RE.pattern).mock(side_effect=ad_page)

        async with Fetcher(3, sleep=no_sleep) as fetcher:
            notifier = Notifier(bot, 1, sleep=no_sleep)  # type: ignore[arg-type]
            poller = Poller(settings, storage, fetcher, notifier)
            environment = Env(storage, poller, bot, pages)
            yield environment
    await storage.close()


async def seed_it(env: Env, *ads: tuple[int, str]) -> None:
    """Первый запуск раздела IT (тихое запоминание), очистить сообщения."""
    env.pages[IT.url] = page(*(ads or ads_range(1, 3)))
    await env.poller.poll_once()
    env.bot.sent.clear()
    env.detail_calls.clear()


async def want_experience(env: Env, *values: str) -> None:
    def mutate(profile: FilterProfile) -> None:
        profile.experience = list(values)

    await env.storage.update_profile(mutate)


# ---------- критерий 9.5 ----------


async def test_details_limited_per_cycle_newest_first(env: Env) -> None:
    await seed_it(env)
    await want_experience(env, "без опыта")
    # 15 новых: чётные — «Без опыта» (подходят), нечётные — «От 1 года»
    for ad_id in range(101, 116):
        exp = "Без опыта" if ad_id % 2 == 0 else "От 1 года"
        env.details[ad_id] = httpx.Response(200, text=detail(experience=exp))
    env.pages[IT.url] = page(*ads_range(101, 115), *ads_range(1, 3))

    stats = await env.poller.poll_once()
    assert stats is not None and stats.details_loaded == 10
    assert env.detail_calls == list(range(115, 105, -1))  # ровно 10, от самых новых
    assert len(env.vacancies()) == 5  # чётные из 106..115

    env.bot.sent.clear()
    env.detail_calls.clear()
    stats = await env.poller.poll_once()
    assert env.detail_calls == [105, 104, 103, 102, 101]  # остаток — в следующем цикле
    assert len(env.vacancies()) == 2  # 104 и 102

    env.bot.sent.clear()
    env.detail_calls.clear()
    await env.poller.poll_once()
    assert env.detail_calls == [] and env.vacancies() == []  # дублей нет


async def test_three_failures_send_with_mark_once(env: Env) -> None:
    await seed_it(env)
    await want_experience(env, "без опыта")
    env.details[201] = httpx.Response(500)
    env.pages[IT.url] = page((201, "Сломанная страница"), *ads_range(1, 3))

    for _ in range(2):
        await env.poller.poll_once()
        assert env.vacancies() == []  # пока пробуем снова
    await env.poller.poll_once()  # третья неудача
    assert len(env.vacancies()) == 1
    assert "Сломанная страница" in env.vacancies()[0]
    assert UNCHECKED_MARK in env.vacancies()[0]

    env.bot.sent.clear()
    calls_before = len(env.detail_calls)
    await env.poller.poll_once()
    assert env.vacancies() == []  # дублей нет
    assert len(env.detail_calls) == calls_before  # больше не запрашиваем


async def test_retry_then_success_is_not_marked(env: Env) -> None:
    await seed_it(env)
    await want_experience(env, "без опыта")
    env.details[301] = httpx.Response(503)
    env.pages[IT.url] = page((301, "Со второго раза"), *ads_range(1, 3))
    await env.poller.poll_once()
    assert env.vacancies() == []
    env.details[301] = httpx.Response(200, text=detail())
    await env.poller.poll_once()
    assert len(env.vacancies()) == 1 and UNCHECKED_MARK not in env.vacancies()[0]


async def test_deleted_ad_is_not_sent(env: Env) -> None:
    await seed_it(env)
    await want_experience(env, "без опыта")
    env.details[401] = httpx.Response(404)
    env.pages[IT.url] = page((401, "Удалено"), *ads_range(1, 3))
    await env.poller.poll_once()
    await env.poller.poll_once()
    assert env.vacancies() == [] and env.detail_calls == [401]


async def test_no_detail_requests_without_schedule_or_experience(env: Env) -> None:
    await seed_it(env)
    env.pages[IT.url] = page(*ads_range(501, 505), *ads_range(1, 3))
    await env.poller.poll_once()
    assert env.detail_calls == []
    assert len(env.vacancies()) == 5


async def test_pending_accepted_when_filter_removed(env: Env) -> None:
    await seed_it(env)

    def experience_and_tiny_limit(profile: FilterProfile) -> None:
        profile.experience = ["без опыта"]

    await env.storage.update_profile(experience_and_tiny_limit)
    env.poller._settings = env.poller._settings.model_copy(update={"max_details_per_cycle": 1})
    env.pages[IT.url] = page(*ads_range(601, 603), *ads_range(1, 3))
    await env.poller.poll_once()
    assert env.detail_calls == [603] and len(env.vacancies()) == 1

    await want_experience(env)  # снять фильтр стажа
    env.bot.sent.clear()
    await env.poller.poll_once()
    assert len(env.vacancies()) == 2  # 601 и 602 из очереди — без страниц
    assert env.detail_calls == [603]


async def test_seed_does_not_load_details(env: Env) -> None:
    await want_experience(env, "без опыта")
    env.pages[IT.url] = page(*ads_range(1, 5))
    await env.poller.poll_once()
    assert env.detail_calls == []
    assert len(env.vacancies()) == 3  # обычный seed


# ---------- разделы по профилю ----------


async def test_only_selected_and_visible_categories_are_polled(env: Env) -> None:
    def only_it(profile: FilterProfile) -> None:
        profile.categories = ["it"]

    await env.storage.update_profile(only_it)
    env.pages[STUDENTS.url] = page(*ads_range(1, 3))
    stats = await env.poller.poll_once()
    assert stats is not None and [c.key for c in stats.categories] == ["it"]

    def invisible(profile: FilterProfile) -> None:
        profile.categories = ["sales"]  # есть в каталоге, но не в VISIBLE_CATEGORIES

    await env.storage.update_profile(invisible)
    stats = await env.poller.poll_once()
    assert stats is not None and stats.categories == []


async def test_old_categories_toggle_updates_profile(env: Env) -> None:
    await env.storage.set_category_enabled("students", False)
    assert (await env.storage.get_filter_profile()).categories == ["it"]
    with pytest.raises(ValueError, match="хотя бы одна"):
        await env.storage.set_category_enabled("it", False)
    await env.storage.set_category_enabled("students", True)
    assert (await env.storage.get_filter_profile()).categories == ["it", "students"]


# ---------- фильтры по карточке и справочник ----------


async def test_city_filter_on_cards(env: Env) -> None:
    await seed_it(env)

    def dushanbe(profile: FilterProfile) -> None:
        profile.cities = ["душанбе"]

    await env.storage.update_profile(dushanbe)
    env.pages[IT.url] = page(
        (701, "Вакансия 701", "Худжанд"),
        (702, "Вакансия 702", "Худжанд"),
        (703, "Вакансия 703", "Душанбе"),
        *ads_range(1, 3),
    )
    await env.poller.poll_once()
    assert len(env.vacancies()) == 1 and "Вакансия 703" in env.vacancies()[0]


async def test_attr_values_collected(env: Env) -> None:
    await seed_it(env)  # города карточек запоминаются уже при seed
    await want_experience(env, "без опыта")
    env.details[801] = httpx.Response(200, text=detail("Удаленно", "От 1 года"))
    env.pages[IT.url] = page((801, "Удалёнка"), *ads_range(1, 3), city="Куляб")
    await env.poller.poll_once()
    cities = {v.value_norm: v.seen_count for v in await env.storage.get_attr_values("city")}
    assert cities == {"душанбе": 3, "кулоб": 1}
    assert [v.label for v in await env.storage.get_attr_values("schedule")] == ["Удаленно"]
    assert [v.label for v in await env.storage.get_attr_values("experience")] == ["От 1 года"]
