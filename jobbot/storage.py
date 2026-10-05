"""Хранилище на SQLite (aiosqlite): схема, миграции и репозиторий объявлений, фильтров, состояния.

Одно подключение на процесс. Все обращения к нему идут под asyncio.Lock, а записи — в явных
транзакциях: так операции разных корутин не перемешиваются внутри одной транзакции.
Весь SQL — только с параметрами (`?`), значения в строки запросов не подставляются.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite

from jobbot.config import Category
from jobbot.matcher import normalize
from jobbot.models import Ad

log = logging.getLogger("storage")

KeywordKind = Literal["include", "exclude"]

# Миграции: номер версии = индекс + 1. Каждая — кортеж SQL-команд, применяется в транзакции.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (  # версия 1 — схема из раздела 7 ТЗ
        """
        CREATE TABLE IF NOT EXISTS ads (
            ad_id         INTEGER PRIMARY KEY,
            category_key  TEXT    NOT NULL,
            title         TEXT    NOT NULL,
            url           TEXT    NOT NULL,
            salary_text   TEXT,
            city          TEXT,
            date_label    TEXT,
            matched       INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT    NOT NULL,
            sent_at       TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_ads_category ON ads(category_key)",
        "CREATE INDEX IF NOT EXISTS idx_ads_seen ON ads(first_seen_at)",
        """
        CREATE TABLE IF NOT EXISTS keywords (
            kind TEXT NOT NULL CHECK (kind IN ('include', 'exclude')),
            word TEXT NOT NULL,
            PRIMARY KEY (kind, word)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS categories (
            key     TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            mode    TEXT NOT NULL DEFAULT 'all' CHECK (mode IN ('all', 'keywords'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    ),
)
SCHEMA_VERSION = len(MIGRATIONS)

# Значение sent_at для объявлений, которые запомнены без отправки (первый запуск раздела).
# Не NULL — значит, досылка их не тронет; меньше любой реальной даты — значит, в счётчик
# «отправлено с …» они не попадут.
SKIPPED_SENT_AT = "0000-00-00T00:00:00Z"

_DEFAULTS_SEEDED = "defaults_seeded"


@dataclass(frozen=True, slots=True)
class CategoryState:
    """Настройки раздела, которые владелец меняет командой /categories."""

    key: str
    enabled: bool
    mode: Literal["all", "keywords"]


def to_iso(moment: datetime | None = None) -> str:
    """Время в формате ISO 8601 UTC (`2026-10-05T14:05:12Z`) — строки сравнимы как текст."""
    moment = moment or datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_ad(row: aiosqlite.Row) -> Ad:
    return Ad(
        ad_id=row["ad_id"],
        url=row["url"],
        title=row["title"],
        category_key=row["category_key"],
        salary_text=row["salary_text"],
        city=row["city"],
        date_label=row["date_label"],
    )


class Storage:
    """Репозиторий. Создавать через `await Storage.open(path)`, закрывать через `close()`."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str | Path) -> Storage:
        """Открыть (и при необходимости создать) базу, включить WAL, применить миграции."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: транзакциями управляем сами (BEGIN/COMMIT в _tx).
        db = await aiosqlite.connect(path, isolation_level=None)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA synchronous = NORMAL")
            storage = cls(db)
            await storage._migrate()
        except BaseException:
            await db.close()
            raise
        return storage

    async def close(self) -> None:
        await self._db.close()

    # ---------- служебное ----------

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """Запись: блокировка + транзакция (COMMIT при успехе, ROLLBACK при любой ошибке)."""
        async with self._lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                await self._db.execute("ROLLBACK")
                raise
            else:
                await self._db.execute("COMMIT")

    async def _fetchall(self, sql: str, params: Sequence[object] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cursor = await self._db.execute(sql, params)
            return list(await cursor.fetchall())

    async def _fetchone(self, sql: str, params: Sequence[object] = ()) -> aiosqlite.Row | None:
        async with self._lock:
            cursor = await self._db.execute(sql, params)
            return await cursor.fetchone()

    async def _migrate(self) -> None:
        """Прочитать `schema_version` и применить недостающие миграции по порядку."""
        async with self._tx() as db:
            await db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            cursor = await db.execute("SELECT MAX(version) FROM schema_version")
            row = await cursor.fetchone()
            current = row[0] if row and row[0] is not None else 0
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"База создана более новой версией бота (схема {current}, "
                    f"эта версия понимает до {SCHEMA_VERSION})"
                )
            for version in range(current + 1, SCHEMA_VERSION + 1):
                log.info("миграция базы: версия %d", version)
                for statement in MIGRATIONS[version - 1]:
                    await db.execute(statement)
                await db.execute("DELETE FROM schema_version")
                await db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))

    async def schema_version(self) -> int:
        row = await self._fetchone("SELECT MAX(version) FROM schema_version")
        return row[0] if row and row[0] is not None else 0

    # ---------- объявления ----------

    async def add_ads(
        self,
        items: Iterable[tuple[Ad, bool]],
        *,
        skip_sending: bool = False,
        now: datetime | None = None,
    ) -> list[Ad]:
        """Сохранить объявления (`INSERT OR IGNORE`) и вернуть только действительно новые.

        items — пары (объявление, прошло ли фильтр). Уже известный `ad_id` не меняется и
        в результат не попадает. `skip_sending=True` — объявления первого запуска: они
        запоминаются как обработанные (`sent_at = SKIPPED_SENT_AT`) и никогда не отправляются,
        но и не считаются «отправленными сегодня» в /status.
        """
        stamp = to_iso(now)
        sent_at = SKIPPED_SENT_AT if skip_sending else None
        new: list[Ad] = []
        async with self._tx() as db:
            for ad, matched in items:
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO ads (ad_id, category_key, title, url, salary_text, "
                    "city, date_label, matched, first_seen_at, sent_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ad.ad_id,
                        ad.category_key,
                        ad.title,
                        ad.url,
                        ad.salary_text,
                        ad.city,
                        ad.date_label,
                        int(matched),
                        stamp,
                        sent_at,
                    ),
                )
                if cursor.rowcount == 1:
                    new.append(ad)
        return new

    async def mark_sent(self, ad_ids: Iterable[int], *, now: datetime | None = None) -> None:
        """Отметить объявления отправленными — вызывать только после успешной отправки."""
        stamp = to_iso(now)
        async with self._tx() as db:
            await db.executemany(
                "UPDATE ads SET sent_at = ? WHERE ad_id = ?",
                [(stamp, ad_id) for ad_id in ad_ids],
            )

    async def unsent_matched(self, since: datetime) -> list[Ad]:
        """Подходящие, но не отправленные объявления, найденные не раньше `since`."""
        rows = await self._fetchall(
            "SELECT ad_id, category_key, title, url, salary_text, city, date_label FROM ads "
            "WHERE matched = 1 AND sent_at IS NULL AND first_seen_at >= ? "
            "ORDER BY first_seen_at, ad_id",
            (to_iso(since),),
        )
        return [_row_to_ad(row) for row in rows]

    async def recent_matched(self, limit: int, category_key: str | None = None) -> list[Ad]:
        """Последние подходящие объявления (новые первыми), опционально по разделу."""
        rows = await self._fetchall(
            "SELECT ad_id, category_key, title, url, salary_text, city, date_label FROM ads "
            "WHERE matched = 1 AND (? IS NULL OR category_key = ?) "
            "ORDER BY first_seen_at DESC, ad_id DESC LIMIT ?",
            (category_key, category_key, limit),
        )
        return [_row_to_ad(row) for row in rows]

    async def has_ads(self, category_key: str) -> bool:
        """Есть ли в базе хоть одно объявление раздела (нет — значит, это первый запуск)."""
        row = await self._fetchone(
            "SELECT 1 FROM ads WHERE category_key = ? LIMIT 1", (category_key,)
        )
        return row is not None

    async def count_ads(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) FROM ads")
        return int(row[0]) if row else 0

    async def count_sent_since(self, since: datetime) -> int:
        row = await self._fetchone("SELECT COUNT(*) FROM ads WHERE sent_at >= ?", (to_iso(since),))
        return int(row[0]) if row else 0

    # ---------- слова фильтра ----------

    async def get_keywords(self, kind: KeywordKind) -> list[str]:
        rows = await self._fetchall(
            "SELECT word FROM keywords WHERE kind = ? ORDER BY word", (kind,)
        )
        return [row["word"] for row in rows]

    async def add_keyword(self, kind: KeywordKind, word: str) -> bool:
        """Добавить слово (хранится нормализованным). False — такое слово уже есть."""
        async with self._tx() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO keywords (kind, word) VALUES (?, ?)", (kind, normalize(word))
            )
            return cursor.rowcount == 1

    async def remove_keyword(self, word: str) -> int:
        """Удалить слово из обоих списков; вернуть, сколько записей удалено."""
        async with self._tx() as db:
            cursor = await db.execute("DELETE FROM keywords WHERE word = ?", (normalize(word),))
            return cursor.rowcount

    async def count_keywords(self, kind: KeywordKind) -> int:
        row = await self._fetchone("SELECT COUNT(*) FROM keywords WHERE kind = ?", (kind,))
        return int(row[0]) if row else 0

    # ---------- разделы ----------

    async def get_categories(self) -> list[CategoryState]:
        rows = await self._fetchall("SELECT key, enabled, mode FROM categories ORDER BY rowid")
        return [CategoryState(r["key"], bool(r["enabled"]), r["mode"]) for r in rows]

    async def set_category_enabled(self, key: str, enabled: bool) -> None:
        async with self._tx() as db:
            await db.execute("UPDATE categories SET enabled = ? WHERE key = ?", (int(enabled), key))

    async def set_category_mode(self, key: str, mode: Literal["all", "keywords"]) -> None:
        async with self._tx() as db:
            await db.execute("UPDATE categories SET mode = ? WHERE key = ?", (mode, key))

    async def seed_defaults(
        self,
        categories: Iterable[Category],
        include: Iterable[str],
        exclude: Iterable[str],
    ) -> None:
        """Заполнить настройки значениями по умолчанию.

        Разделы добавляются при каждом старте, если их ещё нет (новый раздел в конфиге
        появится сам; настройки существующих не трогаются). Слова include/exclude
        записываются один раз: если владелец удалил слово, оно не вернётся после перезапуска.
        """
        async with self._tx() as db:
            for category in categories:
                await db.execute(
                    "INSERT OR IGNORE INTO categories (key, enabled, mode) VALUES (?, 1, ?)",
                    (category.key, category.mode),
                )
            cursor = await db.execute("SELECT 1 FROM state WHERE key = ?", (_DEFAULTS_SEEDED,))
            if await cursor.fetchone() is None:
                for kind, words in (("include", include), ("exclude", exclude)):
                    await db.executemany(
                        "INSERT OR IGNORE INTO keywords (kind, word) VALUES (?, ?)",
                        [(kind, normalize(word)) for word in words],
                    )
                await db.execute(
                    "INSERT INTO state (key, value) VALUES (?, '1')", (_DEFAULTS_SEEDED,)
                )

    # ---------- состояние ----------

    async def get_state(self, key: str, default: str | None = None) -> str | None:
        row = await self._fetchone("SELECT value FROM state WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_state(self, key: str, value: str) -> None:
        async with self._tx() as db:
            await db.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    async def delete_state(self, key: str) -> None:
        async with self._tx() as db:
            await db.execute("DELETE FROM state WHERE key = ?", (key,))


def from_iso(value: str) -> datetime:
    """Обратное к `to_iso`: строка `2026-10-05T14:05:12Z` → datetime в UTC."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
