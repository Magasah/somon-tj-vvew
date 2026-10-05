"""Точка входа: `python -m jobbot [--once] [--dry-run]`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError

from jobbot.bot.handlers import BotDeps, build_dispatcher
from jobbot.config import DEFAULT_EXCLUDE, DEFAULT_INCLUDE, Settings, load_settings
from jobbot.fetcher import Fetcher, FetchError
from jobbot.logging_setup import setup_logging
from jobbot.notifier import Notifier
from jobbot.parser import parse_page
from jobbot.poller import Poller
from jobbot.storage import Storage

log = logging.getLogger("main")

SHUTDOWN_TIMEOUT_SEC = 30  # сколько ждём завершения текущего опроса при остановке


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jobbot",
        description="Telegram-бот уведомлений о новых вакансиях с somon.tj",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="выполнить один опрос сайта и выйти",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="отладка: вывести найденные объявления в консоль, без записи в базу и отправки",
    )
    return parser


def _force_utf8_output() -> None:
    # Консоль Windows может быть в старой кодировке — иначе русский текст ломается.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


async def dry_run(settings: Settings) -> int:
    """Один опрос первой страницы каждого раздела: печать в консоль, без базы и Telegram."""
    exit_code = 0
    async with Fetcher(settings.request_delay_sec) as fetcher:
        for category in settings.categories:
            try:
                html = await fetcher.fetch_page(category.url)
            except FetchError as exc:
                log.error("%s: не удалось загрузить страницу: %s", category.key, exc)
                exit_code = 1
                continue
            result = parse_page(html, category.key)
            mode = " (упрощённый режим)" if result.simplified else ""
            print(f"\n=== {category.title}: найдено {len(result.ads)}{mode} ===")
            for ad in result.ads:
                details = " · ".join(x for x in (ad.salary_text, ad.city, ad.date_label) if x)
                print(f"[{ad.ad_id}] {ad.title}\n    {details}\n    {ad.url}")
    return exit_code


async def run_bot(settings: Settings, *, once: bool) -> int:
    """Рабочий режим: база, опрос сайта, команды бота, отправка владельцу."""
    storage = await Storage.open(settings.db_path)
    fetcher = Fetcher(settings.request_delay_sec)
    bot = Bot(token=settings.bot_token.get_secret_value())
    stop = asyncio.Event()
    try:
        await storage.seed_defaults(settings.categories, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
        notifier = Notifier(
            bot, settings.owner_chat_id or 0, digest_threshold=settings.digest_threshold
        )
        poller = Poller(settings, storage, fetcher, notifier)

        if once:
            await poller.flush_unsent()
            await poller.poll_once()
            return 0

        if not await _check_token(bot):
            return 1
        _install_stop_handlers(stop)
        log.info("Бот запущен, интервал опроса %d с", settings.poll_interval_sec)
        await _serve(settings, storage, poller, notifier, bot, stop)
        log.info("Остановлен")
        return 0
    finally:
        await fetcher.aclose()
        await bot.session.close()
        await storage.close()


async def _check_token(bot: Bot) -> bool:
    """Проверить токен до запуска. Неверный токен — понятная ошибка вместо трассировки."""
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        log.error("Telegram отклонил BOT_TOKEN: проверьте токен у @BotFather и файл .env")
        return False
    except TelegramAPIError as exc:
        log.warning(
            "не удалось проверить токен (%s), продолжаем — Telegram может быть недоступен", exc
        )
        return True
    log.info("Бот: @%s", me.username)
    return True


async def _serve(
    settings: Settings,
    storage: Storage,
    poller: Poller,
    notifier: Notifier,
    bot: Bot,
    stop: asyncio.Event,
) -> None:
    """Опрос сайта и приём команд работают одновременно до сигнала остановки."""
    dispatcher = build_dispatcher(BotDeps(settings, storage, poller, notifier))
    poll_task = asyncio.create_task(poller.run_forever(stop), name="poller")
    bot_task = asyncio.create_task(
        dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False),
        name="telegram-polling",
    )
    stop_task = asyncio.create_task(stop.wait(), name="stop-wait")
    try:
        # Ждём сигнал остановки или падение одной из задач (тогда останавливаем всё).
        await asyncio.wait({poll_task, bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        stop.set()
        log.info("Останавливаюсь: жду конец текущего опроса (до %d с)…", SHUTDOWN_TIMEOUT_SEC)
        if not bot_task.done():
            await dispatcher.stop_polling()
        for task in (poll_task, bot_task):
            if task.done():
                continue
            try:
                await asyncio.wait_for(task, timeout=SHUTDOWN_TIMEOUT_SEC)
            except TimeoutError:
                log.warning(
                    "задача %s не завершилась за %d с, прерываю",
                    task.get_name(),
                    SHUTDOWN_TIMEOUT_SEC,
                )
        for task in (poll_task, bot_task):
            if task.done() and not task.cancelled() and task.exception():
                log.error(
                    "задача %s завершилась ошибкой", task.get_name(), exc_info=task.exception()
                )
    finally:
        stop_task.cancel()


def _install_stop_handlers(stop: asyncio.Event) -> None:
    """Остановка по SIGINT (Ctrl+C) и SIGTERM (docker stop): мягко, через событие `stop`."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: add_signal_handler недоступен — используем обычный обработчик сигналов.
            signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(stop.set))


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)

    # В режиме --dry-run Telegram не нужен, поэтому токен и chat_id не обязательны.
    settings = load_settings(require_telegram=not args.dry_run)
    setup_logging(settings.log_level, settings.tz)

    log.info(
        "Настройки загружены (once=%s, dry_run=%s, разделов: %d)",
        args.once,
        args.dry_run,
        len(settings.categories),
    )
    try:
        if args.dry_run:
            return asyncio.run(dry_run(settings))
        return asyncio.run(run_bot(settings, once=args.once))
    except KeyboardInterrupt:
        log.info("Остановлено (Ctrl+C)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
