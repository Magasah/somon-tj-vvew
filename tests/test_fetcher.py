"""Тесты fetcher: задержка, повторы, 403/429, robots.txt (HTTP мокается через respx)."""

from pathlib import Path

import httpx
import pytest
import respx

from jobbot.config import DEFAULT_CATEGORIES
from jobbot.fetcher import (
    BlockedError,
    Fetcher,
    FetchError,
    RobotsDisallowedError,
    build_page_url,
    check_url_allowed,
)

ROBOTS = (Path(__file__).parent / "fixtures" / "robots.txt").read_text(encoding="utf-8")
URL = DEFAULT_CATEGORIES[0].url
ROBOTS_URL = "https://somon.tj/robots.txt"
MOVED = "https://somon.tj/vakansii/it-novyi-adres/"


class FakeSleep:
    """Подмена asyncio.sleep: запоминает паузы и не ждёт по-настоящему."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleep() -> FakeSleep:
    return FakeSleep()


def make_fetcher(sleep: FakeSleep) -> Fetcher:
    return Fetcher(3, sleep=sleep)


def test_build_page_url() -> None:
    assert build_page_url(URL) == URL
    assert build_page_url(URL, 2) == URL + "?page=2"
    with pytest.raises(ValueError):
        build_page_url(URL, 0)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/vakansii/",
        "http://somon.tj/vakansii/",
        "https://somon.tj/api/v1/x",
        "https://somon.tj/admin",
        "https://somon.tj/profile/me",
        "https://somon.tj/vakansii/?ordering=price",
        "https://somon.tj/vakansii/?price_min=1",
        "https://somon.tj/vakansii/?attrs__x=1",
        "https://somon.tj/vakansii/a---b/",
    ],
)
def test_forbidden_urls_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        check_url_allowed(url)


def test_allowed_urls() -> None:
    for category in DEFAULT_CATEGORIES:
        check_url_allowed(category.url)
        check_url_allowed(category.url + "?page=3")


@respx.mock
async def test_fetch_ok_and_robots_cached(sleep: FakeSleep) -> None:
    robots = respx.get(ROBOTS_URL).respond(200, text=ROBOTS)
    page = respx.get(URL).respond(200, text="<html>ok</html>")
    async with make_fetcher(sleep) as fetcher:
        assert await fetcher.fetch_page(URL) == "<html>ok</html>"
        await fetcher.fetch_page(URL)
    assert robots.call_count == 1  # robots.txt загружен один раз
    assert page.call_count == 2
    assert page.calls[0].request.headers["user-agent"] == "SomonJobAlertBot/1.0 (personal use)"


@respx.mock
async def test_delay_between_requests(sleep: FakeSleep) -> None:
    respx.get(ROBOTS_URL).respond(200, text=ROBOTS)
    respx.get(URL).respond(200, text="x")
    async with make_fetcher(sleep) as fetcher:
        await fetcher.fetch_page(URL)
        await fetcher.fetch_page(URL)
    # Запросов три (robots + 2 страницы): перед 2-м и 3-м ждём почти полные 3 секунды.
    assert len(sleep.calls) == 2
    assert all(2.5 < s <= 3 for s in sleep.calls)


@respx.mock
async def test_5xx_retried_then_ok(sleep: FakeSleep) -> None:
    respx.get(ROBOTS_URL).respond(200, text=ROBOTS)
    page = respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(500), httpx.Response(200, text="ok")]
    )
    async with make_fetcher(sleep) as fetcher:
        assert await fetcher.fetch_page(URL) == "ok"
    assert page.call_count == 3
    # Паузы: задержка перед 1-м запросом страницы (~3 с), повтор ~2 с, повтор ~4 с.
    assert any(2 <= s < 3 for s in sleep.calls)
    assert any(4 <= s < 5 for s in sleep.calls)


@respx.mock
async def test_network_errors_give_up_after_retries(sleep: FakeSleep) -> None:
    respx.get(ROBOTS_URL).respond(200, text=ROBOTS)
    page = respx.get(URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
    async with make_fetcher(sleep) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.fetch_page(URL)
    assert page.call_count == 4  # первая попытка + 3 повтора


@pytest.mark.parametrize("status", [403, 429])
@respx.mock
async def test_blocked_no_retry(sleep: FakeSleep, status: int) -> None:
    respx.get(ROBOTS_URL).respond(200, text=ROBOTS)
    page = respx.get(URL).respond(status)
    async with make_fetcher(sleep) as fetcher:
        with pytest.raises(BlockedError) as exc:
            await fetcher.fetch_page(URL)
    assert exc.value.status_code == status
    assert page.call_count == 1


@respx.mock
async def test_404_is_error_without_retry(sleep: FakeSleep) -> None:
    respx.get(ROBOTS_URL).respond(200, text=ROBOTS)
    page = respx.get(URL).respond(404)
    async with make_fetcher(sleep) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.fetch_page(URL)
    assert page.call_count == 1


@respx.mock
async def test_robots_disallow_blocks_request(sleep: FakeSleep) -> None:
    respx.get(ROBOTS_URL).respond(200, text="User-agent: *\nDisallow: /vakansii/\n")
    page = respx.get(URL).respond(200, text="x")
    async with make_fetcher(sleep) as fetcher:
        with pytest.raises(RobotsDisallowedError):
            await fetcher.fetch_page(URL)
    assert page.call_count == 0


@respx.mock
async def test_real_robots_allows_default_categories(sleep: FakeSleep) -> None:
    respx.get(ROBOTS_URL).respond(200, text=ROBOTS)
    async with make_fetcher(sleep) as fetcher:
        await fetcher.refresh_robots()
        for category in DEFAULT_CATEGORIES:
            assert fetcher.is_allowed(category.url)
            assert fetcher.is_allowed(category.url + "?page=2")
        assert not fetcher.is_allowed("https://somon.tj/api/x")


@respx.mock
async def test_missing_robots_means_no_restrictions(sleep: FakeSleep) -> None:
    respx.get(ROBOTS_URL).respond(404)
    respx.get(URL).respond(200, text="ok")
    async with make_fetcher(sleep) as fetcher:
        assert await fetcher.fetch_page(URL) == "ok"


@respx.mock
async def test_redirect_inside_site_is_followed(sleep: FakeSleep) -> None:
    respx.get(ROBOTS_URL).respond(200, text=ROBOTS)
    respx.get(URL).respond(301, headers={"Location": MOVED})
    respx.get(MOVED).respond(200, text="ok")
    async with make_fetcher(sleep) as fetcher:
        assert await fetcher.fetch_page(URL) == "ok"


@pytest.mark.parametrize(
    "target", ["https://evil.example/vakansii/", "https://somon.tj/vakansii/?ordering=price"]
)
@respx.mock
async def test_redirect_to_forbidden_target_is_rejected(sleep: FakeSleep, target: str) -> None:
    respx.get(ROBOTS_URL).respond(200, text=ROBOTS)
    respx.get(URL).respond(302, headers={"Location": target})
    respx.get(target).respond(200, text="чужая страница")
    async with make_fetcher(sleep) as fetcher:
        with pytest.raises(FetchError, match="перенаправил"):
            await fetcher.fetch_page(URL)
