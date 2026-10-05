"""Настройка логов: формат с временем в часовом поясе владельца и маскировка токена."""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Токен Telegram-бота выглядит как 123456789:AAH... (35 символов после двоеточия).
TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]{35}")
MASK = "***TOKEN***"

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def mask_secrets(text: str) -> str:
    """Заменить всё, что похоже на токен бота, на заглушку."""
    return TOKEN_RE.sub(MASK, text)


class SafeFormatter(logging.Formatter):
    """Форматтер: время по заданному поясу и маскировка токена во всём выводе.

    Маскируется итоговая строка целиком, поэтому токен не просочится
    ни через сообщение, ни через аргументы, ни через текст исключения.
    """

    def __init__(self, tz_name: str) -> None:
        super().__init__(LOG_FORMAT, DATE_FORMAT)
        self._tz = ZoneInfo(tz_name)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, self._tz)
        return moment.strftime(datefmt or DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        return mask_secrets(super().format(record))


class TokenMaskFilter(logging.Filter):
    """Фильтр: маскирует токен в сообщении и аргументах записи (на случай чужих handler'ов)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_secrets(record.getMessage())
        record.args = None
        return True


def setup_logging(level: str = "INFO", tz_name: str = "Asia/Dushanbe") -> None:
    """Настроить корневой логгер: вывод в stderr, формат из ТЗ, маскировка токена."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SafeFormatter(tz_name))
    handler.addFilter(TokenMaskFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # httpx на уровне INFO пишет полный URL запроса — для Telegram там лежит токен.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
