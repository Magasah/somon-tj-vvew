"""Настройки бота: чтение из .env, валидация, список разделов сайта."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_URL = "https://somon.tj"
USER_AGENT = "SomonJobAlertBot/1.0 (personal use)"

# Минимальные значения защищают сайт от слишком частых запросов (раздел 2.3 ТЗ).
MIN_POLL_INTERVAL_SEC = 180
MIN_REQUEST_DELAY_SEC = 3


@dataclass(frozen=True)
class Category:
    """Раздел вакансий на сайте. Новый раздел = одна строка в DEFAULT_CATEGORIES."""

    key: str
    title: str
    url: str
    mode: Literal["all", "keywords"] = "all"


DEFAULT_CATEGORIES: tuple[Category, ...] = (
    Category(
        key="it",
        title="IT, телеком, компьютеры",
        url=f"{BASE_URL}/vakansii/it--telekom--kompyuteryi/",
    ),
    Category(
        key="students",
        title="Начало карьеры, студенты",
        url=f"{BASE_URL}/vakansii/nachalo-kareryi--studentyi/",
    ),
)

DEFAULT_INCLUDE: tuple[str, ...] = (
    "python", "frontend", "backend", "разработчик", "программист", "developer",
    "junior", "стажер", "стажировка", "без опыта", "web", "react", "django",
    "fastapi", "1с", "it",
)  # fmt: skip

DEFAULT_EXCLUDE: tuple[str, ...] = (
    "колл-центр", "колл центр", "call center", "оператор колл",
)  # fmt: skip


class Settings(BaseSettings):
    """Все параметры из .env (имена переменных — как в .env.example, без учёта регистра)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr = SecretStr("")
    owner_chat_id: int | None = None
    poll_interval_sec: int = 300
    poll_jitter_sec: int = 60
    request_delay_sec: float = 3
    max_pages: int = 3
    seed_send_last: int = 3
    digest_threshold: int = 8
    fetch_details: bool = False
    db_path: str = "data/jobbot.db"
    log_level: str = "INFO"
    tz: str = "Asia/Dushanbe"

    categories: tuple[Category, ...] = DEFAULT_CATEGORIES

    @field_validator("owner_chat_id", mode="before")
    @classmethod
    def _empty_chat_id_is_none(cls, value: object) -> object:
        # Пустая строка в .env (OWNER_CHAT_ID=) означает «не задано».
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("poll_interval_sec")
    @classmethod
    def _check_interval(cls, value: int) -> int:
        if value < MIN_POLL_INTERVAL_SEC:
            raise ValueError(f"не меньше {MIN_POLL_INTERVAL_SEC} секунд (3 минуты)")
        return value

    @field_validator("poll_jitter_sec", "seed_send_last")
    @classmethod
    def _check_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("не может быть отрицательным")
        return value

    @field_validator("request_delay_sec")
    @classmethod
    def _check_delay(cls, value: float) -> float:
        if value < MIN_REQUEST_DELAY_SEC:
            raise ValueError(f"не меньше {MIN_REQUEST_DELAY_SEC} секунд")
        return value

    @field_validator("max_pages")
    @classmethod
    def _check_pages(cls, value: int) -> int:
        if not 1 <= value <= 5:
            raise ValueError("должно быть от 1 до 5")
        return value

    @field_validator("digest_threshold")
    @classmethod
    def _check_digest(cls, value: int) -> int:
        if value < 1:
            raise ValueError("должно быть не меньше 1")
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("допустимо: DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return level


def load_settings(*, require_telegram: bool = True) -> Settings:
    """Загрузить и проверить настройки. При ошибке — понятное сообщение и выход с кодом 1.

    require_telegram=False нужен для `--dry-run`: он не обращается к Telegram,
    поэтому токен и chat_id не обязательны.
    """
    try:
        settings = Settings()
    except ValidationError as exc:
        lines = ["Ошибка в настройках (.env):"]
        for err in exc.errors():
            name = ".".join(str(part) for part in err["loc"]).upper()
            lines.append(f"  • {name}: {err['msg']}")
        _fail(lines)

    problems: list[str] = []
    if require_telegram:
        if not settings.bot_token.get_secret_value().strip():
            problems.append("  • BOT_TOKEN: не задан (получите у @BotFather)")
        if settings.owner_chat_id is None:
            problems.append("  • OWNER_CHAT_ID: не задан (целое число, ваш chat_id)")
    if problems:
        _fail(["Ошибка в настройках (.env):", *problems])
    return settings


def _fail(lines: list[str]) -> None:
    print("\n".join(lines), file=sys.stderr)
    raise SystemExit(1)
