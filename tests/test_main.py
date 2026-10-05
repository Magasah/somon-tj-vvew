"""Тест запуска и мягкой остановки: опрос и приём команд работают вместе и гаснут по сигналу."""

import asyncio
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.methods import DeleteWebhook, GetMe, GetUpdates
from aiogram.types import User

from jobbot.__main__ import _serve
from jobbot.config import DEFAULT_CATEGORIES, DEFAULT_EXCLUDE, DEFAULT_INCLUDE, Settings
from jobbot.notifier import Notifier
from jobbot.poller import Poller
from jobbot.storage import Storage


class QuietFetcher:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch_page(self, category_url: str, page_no: int = 1) -> str:
        self.calls += 1
        return "<html></html>"


async def test_serve_runs_both_loops_and_stops_gracefully(tmp_path: Path) -> None:
    updates_requested = 0

    async def fake_request(bot: Bot, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        nonlocal updates_requested
        if isinstance(method, GetMe):
            return User(id=999, is_bot=True, first_name="bot", username="somon_test_bot")
        if isinstance(method, DeleteWebhook):
            return True
        if isinstance(method, GetUpdates):
            updates_requested += 1
            await asyncio.sleep(0.05)
            return []
        raise AssertionError(f"неожиданный вызов Telegram: {method}")

    bot = Bot(token="123456789:" + "A" * 35)
    bot.session.make_request = fake_request  # type: ignore[method-assign]
    storage = await Storage.open(tmp_path / "jobbot.db")
    await storage.seed_defaults(DEFAULT_CATEGORIES, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    settings = Settings(_env_file=None, bot_token="x", owner_chat_id=1)  # noqa: S106
    fetcher = QuietFetcher()
    notifier = Notifier(bot, 1)
    poller = Poller(settings, storage, fetcher, notifier)  # type: ignore[arg-type]
    stop = asyncio.Event()

    try:
        serving = asyncio.create_task(_serve(settings, storage, poller, notifier, bot, stop))
        await asyncio.sleep(0.4)
        assert not serving.done()
        assert fetcher.calls == 2  # первый опрос обоих разделов выполнен
        assert updates_requested >= 1  # бот принимает сообщения

        stop.set()
        await asyncio.wait_for(serving, timeout=10)  # остановились сами, без зависаний
        assert not poller.busy
    finally:
        await bot.session.close()
        await storage.close()
