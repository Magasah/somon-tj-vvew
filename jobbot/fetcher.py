"""Загрузка страниц somon.tj: вежливый клиент (задержка, повторы, robots.txt)."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from types import TracebackType
from urllib.parse import parse_qsl, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from jobbot.config import BASE_URL, MIN_REQUEST_DELAY_SEC, USER_AGENT

log = logging.getLogger("fetcher")

ROBOTS_URL = f"{BASE_URL}/robots.txt"
ROBOTS_TTL_SEC = 24 * 60 * 60  # раз в сутки перечитываем robots.txt
RETRY_DELAYS_SEC = (2, 4, 8)  # паузы перед повторами (плюс случайный джиттер)
REQUEST_TIMEOUT_SEC = 15

# Параметры, которые закрыты в robots.txt: их в запросах быть не должно.
FORBIDDEN_PARAMS = ("ordering", "price_min", "price_max", "attrs__")
# Пути, закрытые в robots.txt (дополнительная защита поверх проверки robotparser).
FORBIDDEN_PATHS = ("/api", "/admin", "/profile", "/saved_search/", "/mobile_search/")


class FetchError(Exception):
    """Не удалось загрузить страницу (сеть, таймаут, 5xx после всех повторов)."""


class HttpStatusError(FetchError):
    """Сайт ответил кодом 4xx (кроме 403/429): повторять бесполезно."""

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"HTTP {status_code} для {url}")
        self.status_code = status_code
        self.url = url


class BlockedError(FetchError):
    """Сайт ответил 403 или 429: нужна пауза (раздел 2.3 ТЗ), повторы запрещены."""

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"HTTP {status_code} для {url}")
        self.status_code = status_code
        self.url = url


class RobotsDisallowedError(FetchError):
    """robots.txt запрещает этот URL — запрос не отправляется."""


def build_page_url(category_url: str, page: int = 1) -> str:
    """URL страницы раздела: чистый URL раздела, для страницы 2+ добавляется `?page=N`."""
    if page < 1:
        raise ValueError("номер страницы должен быть не меньше 1")
    return category_url if page == 1 else f"{category_url}?page={page}"


def check_url_allowed(url: str) -> None:
    """Быстрая проверка по правилам ТЗ: домен somon.tj, без запрещённых путей и параметров.

    Бросает ValueError, если URL нарушает правила.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != urlsplit(BASE_URL).netloc:
        raise ValueError(f"запрос разрешён только к {BASE_URL}: {url}")
    if any(parts.path == p.rstrip("/") or parts.path.startswith(p) for p in FORBIDDEN_PATHS):
        raise ValueError(f"путь закрыт в robots.txt: {parts.path}")
    if "---" in url:
        raise ValueError(f"URL с «---» закрыт в robots.txt: {url}")
    for name, _ in parse_qsl(parts.query, keep_blank_values=True):
        if name != "page":
            raise ValueError(f"параметр запроса не разрешён: {name}")


class Fetcher:
    """Один переиспользуемый httpx-клиент; запросы строго последовательны, не чаще 1 в N секунд."""

    def __init__(
        self,
        request_delay_sec: float = MIN_REQUEST_DELAY_SEC,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,  # в тестах подменяется
    ) -> None:
        self._delay = max(request_delay_sec, MIN_REQUEST_DELAY_SEC)
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SEC,
            follow_redirects=True,
        )
        self._sleep = sleep
        self._lock = asyncio.Lock()  # гарантирует отсутствие параллельных запросов
        self._last_request_at: float | None = None
        self._robots: RobotFileParser | None = None
        self._robots_loaded_at: float = 0.0

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_page(self, category_url: str, page: int = 1) -> str:
        """Загрузить страницу раздела и вернуть HTML.

        Перед запросом проверяет правила ТЗ и robots.txt.
        """
        url = build_page_url(category_url, page)
        check_url_allowed(url)
        await self._ensure_robots()
        if not self._robots_allows(url):
            raise RobotsDisallowedError(f"robots.txt запрещает {url}")
        return await self._get(url)

    async def refresh_robots(self) -> None:
        """Принудительно перечитать robots.txt."""
        parser = RobotFileParser()
        try:
            text = await self._get(ROBOTS_URL)
        except HttpStatusError as exc:
            # Нет robots.txt (404 и т.п.) — по стандарту ограничений нет.
            log.warning("robots.txt недоступен (HTTP %d), считаем ограничений нет", exc.status_code)
            text = ""
        parser.parse(text.splitlines())
        self._robots = parser
        self._robots_loaded_at = time.monotonic()
        log.info("robots.txt загружен")

    def is_allowed(self, url: str) -> bool:
        """Разрешён ли URL по загруженному robots.txt (до загрузки — False)."""
        return self._robots is not None and self._robots_allows(url)

    async def _ensure_robots(self) -> None:
        stale = time.monotonic() - self._robots_loaded_at > ROBOTS_TTL_SEC
        if self._robots is None or stale:
            try:
                await self.refresh_robots()
            except FetchError:
                if self._robots is None:
                    raise  # правил нет вовсе — осторожность важнее, не запрашиваем
                log.warning("не удалось обновить robots.txt, используем прежний")

    def _robots_allows(self, url: str) -> bool:
        return self._robots is None or self._robots.can_fetch(USER_AGENT, url)

    async def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = self._delay - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await self._sleep(wait)
        self._last_request_at = time.monotonic()

    async def _get(self, url: str) -> str:
        """GET с задержкой между запросами и повторами на сетевые ошибки и 5xx."""
        last_error: Exception | None = None
        for attempt in range(len(RETRY_DELAYS_SEC) + 1):
            if attempt:
                pause = RETRY_DELAYS_SEC[attempt - 1] + random.uniform(0, 1)  # noqa: S311
                log.warning("повтор %d через %.1f с: %s", attempt, pause, last_error)
                await self._sleep(pause)
            async with self._lock:
                await self._throttle()
                try:
                    response = await self._client.get(url)
                except httpx.HTTPError as exc:  # таймауты и сетевые ошибки
                    last_error = exc
                    self._last_request_at = time.monotonic()
                    continue
                self._last_request_at = time.monotonic()
            if response.status_code in (403, 429):
                raise BlockedError(response.status_code, url)
            if response.status_code >= 500:
                last_error = FetchError(f"HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                raise HttpStatusError(response.status_code, url)
            if response.history:
                # Был редирект: итоговый адрес тоже должен быть разрешённой страницей somon.tj.
                try:
                    check_url_allowed(str(response.url))
                except ValueError as exc:
                    raise FetchError(f"сайт перенаправил на недопустимый адрес: {exc}") from exc
            return response.text
        raise FetchError(f"не удалось загрузить {url}: {last_error}")
