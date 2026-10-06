"""Хранилище на SQLite (aiosqlite): схема, миграции и репозиторий объявлений, фильтров, состояния.

Одно подключение на процесс. Все обращения к нему идут под asyncio.Lock, а записи — в явных
транзакциях: так операции разных корутин не перемешиваются внутри одной транзакции.
Весь SQL — только с параметрами (`?`), значения в строки запросов не подставляются.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite

from jobbot.catalog import CATALOG_BY_KEY
from jobbot.config import Category
from jobbot.filters import FilterProfile, dump_profile, load_profile
from jobbot.matcher import normalize
from jobbot.models import Ad, AdDetails
from jobbot.normalize import normalize_city, normalize_text
from jobbot.parser import parse_salary

log = logging.getLogger("storage")

KeywordKind = Literal["include", "exclude"]
AttrName = Literal["city", "schedule", "experience"]
ATTR_LABEL_MAX = 60

# Версия 1 — схема из раздела 7 ТЗ.
_V1_SQL: tuple[str, ...] = (
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
)

# Версия 2 (v1.1, фильтры через кнопки) — раздел 4.2 TZ_update_filters.md.
# Столбцы добавляются только если их ещё нет: миграция переживает повторный запуск.
_V2_ADS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("salary_min", "INTEGER"),
    ("salary_max", "INTEGER"),
    ("salary_negotiable", "INTEGER NOT NULL DEFAULT 0"),
    ("city_norm", "TEXT"),
    ("schedule", "TEXT"),  # исходный текст с сайта
    ("experience", "TEXT"),
    ("company", "TEXT"),
    ("sphere", "TEXT"),
    ("details_status", "TEXT NOT NULL DEFAULT 'none'"),  # none|pending|ok|failed|skipped
    ("details_attempts", "INTEGER NOT NULL DEFAULT 0"),
)
_V2_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_ads_details ON ads(details_status)",
    """
    CREATE TABLE IF NOT EXISTS filter_profile (
        id   INTEGER PRIMARY KEY CHECK (id = 1),
        data TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attr_values (
        attr       TEXT NOT NULL CHECK (attr IN ('city', 'schedule', 'experience')),
        value_norm TEXT NOT NULL,
        label      TEXT NOT NULL,
        seen_count INTEGER NOT NULL DEFAULT 1,
        last_seen  TEXT NOT NULL,
        PRIMARY KEY (attr, value_norm)
    )
    """,
)


async def _migration_v1(db: aiosqlite.Connection) -> None:
    for statement in _V1_SQL:
        await db.execute(statement)


async def _migration_v2(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(ads)")
    existing = {row[1] for row in await cursor.fetchall()}
    for name, declaration in _V2_ADS_COLUMNS:
        if name not in existing:
            # Имя и тип — константы из кода выше, не данные пользователя.
            await db.execute(f"ALTER TABLE ads ADD COLUMN {name} {declaration}")
    for statement in _V2_SQL:
        await db.execute(statement)

    # Разобрать зарплату и город у объявлений, сохранённых до v1.1 (для подсчётов в меню).
    cursor = await db.execute("SELECT ad_id, salary_text, city FROM ads")
    for ad_id, salary_text, city in await cursor.fetchall():
        salary = parse_salary(salary_text)
        await db.execute(
            "UPDATE ads SET salary_min = ?, salary_max = ?, salary_negotiable = ?, city_norm = ? "
            "WHERE ad_id = ?",
            (
                salary.min,
                salary.max,
                int(salary.negotiable),
                normalize_city(city) if city else None,
                ad_id,
            ),
        )

    # Перенос старых настроек: включённые разделы из `categories` → профиль фильтров.
    cursor = await db.execute("SELECT key FROM categories WHERE enabled = 1 ORDER BY rowid")
    keys = [row[0] for row in await cursor.fetchall() if row[0] in CATALOG_BY_KEY]
    profile = FilterProfile(categories=keys) if keys else FilterProfile()
    await db.execute(
        "INSERT OR IGNORE INTO filter_profile (id, data) VALUES (1, ?)", (dump_profile(profile),)
    )


# Миграции по порядку: номер версии = индекс + 1. Каждая выполняется в общей транзакции.
MIGRATIONS: tuple[Callable[[aiosqlite.Connection], Awaitable[None]], ...] = (
    _migration_v1,
    _migration_v2,
)
SCHEMA_VERSION = len(MIGRATIONS)

# Значение sent_at для объявлений, которые запомнены без отправки (первый запуск раздела).
# Не NULL — значит, досылка их не тронет; меньше любой реальной даты — значит, в счётчик
# «отправлено с …» они не попадут.
SKIPPED_SENT_AT = "0000-00-00T00:00:00Z"

_DEFAULTS_SEEDED = "defaults_seeded"


@dataclass(frozen=True, slots=True)
class AttrValue:
    """Значение из справочника `attr_values` (для кнопок меню фильтров)."""

    value_norm: str
    label: str
    seen_count: int


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


MAX_DETAIL_ATTEMPTS = 3  # после стольких неудач страница объявления считается непроверенной


@dataclass(frozen=True, slots=True)
class PendingDetail:
    """Объявление в очереди на загрузку страницы (график/стаж)."""

    ad: Ad
    attempts: int


def _row_to_ad(row: aiosqlite.Row) -> Ad:
    return Ad(
        ad_id=row["ad_id"],
        url=row["url"],
        title=row["title"],
        category_key=row["category_key"],
        salary_text=row["salary_text"],
        city=row["city"],
        date_label=row["date_label"],
        schedule=row["schedule"],
        experience=row["experience"],
        company=row["company"],
        sphere=row["sphere"],
        details_status=row["details_status"],
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
            current = await storage.schema_version()
            if str(path) != ":memory:" and 0 < current < SCHEMA_VERSION:
                await backup_database(Path(path))
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
                await MIGRATIONS[version - 1](db)
                await db.execute("DELETE FROM schema_version")
                await db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))

    async def schema_version(self) -> int:
        """Текущая версия схемы (0 — база пустая, таблицы версий ещё нет)."""
        exists = await self._fetchone(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        )
        if exists is None:
            return 0
        row = await self._fetchone("SELECT MAX(version) FROM schema_version")
        return row[0] if row and row[0] is not None else 0

    # ---------- профиль фильтров (v1.1) ----------

    async def get_filter_profile(self) -> FilterProfile:
        row = await self._fetchone("SELECT data FROM filter_profile WHERE id = 1")
        return load_profile(row["data"] if row else None)

    async def update_profile(
        self, mutator: Callable[[FilterProfile], FilterProfile | None]
    ) -> FilterProfile:
        """Единственный способ менять профиль: прочитать → изменить → проверить → записать.

        Всё под общей блокировкой и в одной транзакции, поэтому быстрые двойные нажатия
        кнопок не теряют изменения. `mutator` получает копию профиля и может менять её
        на месте или вернуть новый профиль. Недопустимый результат → ValidationError,
        в базе ничего не меняется.
        """
        async with self._tx() as db:
            cursor = await db.execute("SELECT data FROM filter_profile WHERE id = 1")
            row = await cursor.fetchone()
            draft = load_profile(row["data"] if row else None).model_copy(deep=True)
            changed = mutator(draft) or draft
            profile = FilterProfile.model_validate(changed.model_dump())
            await db.execute(
                "INSERT INTO filter_profile (id, data) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (dump_profile(profile),),
            )
        return profile

    # ---------- справочник значений для кнопок (v1.1) ----------

    async def record_attr_values(
        self, attr: AttrName, labels: Iterable[str | None], *, now: datetime | None = None
    ) -> None:
        """Запомнить встреченные на сайте значения города/графика/стажа.

        Повтор того же значения увеличивает `seen_count` (по нему сортируются кнопки).
        Подпись (`label`) остаётся та, что встретилась первой.
        """
        stamp = to_iso(now)
        rows = []
        for label in labels:
            if not label:
                continue
            clean = " ".join(label.split())[:ATTR_LABEL_MAX]
            value_norm = normalize_city(clean) if attr == "city" else normalize_text(clean)
            if value_norm:
                rows.append((attr, value_norm, clean, stamp))
        if not rows:
            return
        async with self._tx() as db:
            await db.executemany(
                "INSERT INTO attr_values (attr, value_norm, label, seen_count, last_seen) "
                "VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(attr, value_norm) DO UPDATE SET "
                "seen_count = seen_count + 1, last_seen = excluded.last_seen",
                rows,
            )

    async def get_attr_values(self, attr: AttrName, limit: int = 50) -> list[AttrValue]:
        """Значения для кнопок: самые частые первыми."""
        rows = await self._fetchall(
            "SELECT value_norm, label, seen_count FROM attr_values WHERE attr = ? "
            "ORDER BY seen_count DESC, label LIMIT ?",
            (attr, limit),
        )
        return [AttrValue(r["value_norm"], r["label"], r["seen_count"]) for r in rows]

    # ---------- объявления ----------

    async def add_ads(
        self,
        items: Iterable[tuple[Ad, bool]],
        *,
        skip_sending: bool = False,
        details_status: Mapping[int, str] | None = None,
        now: datetime | None = None,
    ) -> list[Ad]:
        """Сохранить объявления (`INSERT OR IGNORE`) и вернуть только действительно новые.

        items — пары (объявление, прошло ли фильтр). Уже известный `ad_id` не меняется и
        в результат не попадает. `skip_sending=True` — объявления первого запуска: они
        запоминаются как обработанные (`sent_at = SKIPPED_SENT_AT`) и никогда не отправляются,
        но и не считаются «отправленными сегодня» в /status.
        `details_status` — статус очереди страниц по `ad_id` (`pending`/`skipped`, по умолчанию
        `skipped`).
        """
        stamp = to_iso(now)
        sent_at = SKIPPED_SENT_AT if skip_sending else None
        new: list[Ad] = []
        async with self._tx() as db:
            for ad, matched in items:
                salary = parse_salary(ad.salary_text)
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO ads (ad_id, category_key, title, url, salary_text, "
                    "city, date_label, matched, first_seen_at, sent_at, "
                    "salary_min, salary_max, salary_negotiable, city_norm, details_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        salary.min,
                        salary.max,
                        int(salary.negotiable),
                        ad.city_norm,
                        (details_status or {}).get(ad.ad_id, "skipped"),
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

    # ---------- очередь страниц объявлений (v1.1) ----------

    async def pending_details(self, since: datetime, limit: int) -> list[PendingDetail]:
        """Объявления, ждущие загрузки страницы: новые первыми, не старше `since`."""
        rows = await self._fetchall(
            "SELECT ad_id, category_key, title, url, salary_text, city, date_label, schedule, "
            "experience, company, sphere, details_status, details_attempts FROM ads "
            "WHERE details_status = 'pending' AND first_seen_at >= ? "
            "ORDER BY first_seen_at DESC, ad_id DESC LIMIT ?",
            (to_iso(since), limit),
        )
        return [PendingDetail(_row_to_ad(r), r["details_attempts"]) for r in rows]

    async def expire_pending_details(self, before: datetime) -> int:
        """Старые объявления из очереди больше не проверяем (их уже не отправить)."""
        async with self._tx() as db:
            cursor = await db.execute(
                "UPDATE ads SET details_status = 'skipped' "
                "WHERE details_status = 'pending' AND first_seen_at < ?",
                (to_iso(before),),
            )
            return cursor.rowcount

    async def accept_pending_without_details(self) -> int:
        """График/стаж сняли с фильтра — ожидающие объявления прошли карточку и подходят."""
        async with self._tx() as db:
            cursor = await db.execute(
                "UPDATE ads SET details_status = 'skipped', matched = 1 "
                "WHERE details_status = 'pending'"
            )
            return cursor.rowcount

    async def save_details(self, ad_id: int, details: AdDetails, *, matched: bool) -> None:
        """Страница загружена: сохранить поля и решение по графику/стажу."""
        async with self._tx() as db:
            await db.execute(
                "UPDATE ads SET schedule = ?, experience = ?, company = ?, sphere = ?, "
                "city_norm = COALESCE(city_norm, ?), details_status = 'ok', matched = ? "
                "WHERE ad_id = ?",
                (
                    details.schedule,
                    details.experience,
                    details.company,
                    details.sphere,
                    normalize_city(details.city) if details.city else None,
                    int(matched),
                    ad_id,
                ),
            )

    async def detail_failed(self, ad_id: int, *, permanent: bool = False) -> int:
        """Страница не загрузилась: +1 попытка; на 3-й (или сразу при `permanent`) — `failed`.

        `failed` → объявление подходит (`matched = 1`) и уйдёт с пометкой «не проверено»:
        лучше лишнее уведомление, чем пропущенная вакансия. Возвращает число попыток.
        """
        async with self._tx() as db:
            cursor = await db.execute("SELECT details_attempts FROM ads WHERE ad_id = ?", (ad_id,))
            row = await cursor.fetchone()
            attempts = (row[0] if row else 0) + 1
            if permanent or attempts >= MAX_DETAIL_ATTEMPTS:
                await db.execute(
                    "UPDATE ads SET details_attempts = ?, details_status = 'failed', matched = 1 "
                    "WHERE ad_id = ?",
                    (attempts, ad_id),
                )
            else:
                await db.execute(
                    "UPDATE ads SET details_attempts = ? WHERE ad_id = ?", (attempts, ad_id)
                )
            return attempts

    async def drop_pending_detail(self, ad_id: int) -> None:
        """Объявление удалено с сайта (404): не проверяем и не отправляем."""
        async with self._tx() as db:
            await db.execute(
                "UPDATE ads SET details_status = 'failed', matched = 0 WHERE ad_id = ?", (ad_id,)
            )

    async def unsent_matched(self, since: datetime) -> list[Ad]:
        """Подходящие, но не отправленные объявления, найденные не раньше `since`."""
        rows = await self._fetchall(
            "SELECT ad_id, category_key, title, url, salary_text, city, date_label, schedule, "
            "experience, company, sphere, details_status FROM ads "
            "WHERE matched = 1 AND sent_at IS NULL AND first_seen_at >= ? "
            "ORDER BY first_seen_at, ad_id",
            (to_iso(since),),
        )
        return [_row_to_ad(row) for row in rows]

    async def recent_matched(self, limit: int, category_key: str | None = None) -> list[Ad]:
        """Последние подходящие объявления (новые первыми), опционально по разделу."""
        rows = await self._fetchall(
            "SELECT ad_id, category_key, title, url, salary_text, city, date_label, schedule, "
            "experience, company, sphere, details_status FROM ads "
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
        """Разделы из `/categories`. С v1.1 «включён» = выбран в профиле фильтров."""
        rows = await self._fetchall("SELECT key, enabled, mode FROM categories ORDER BY rowid")
        selected = set((await self.get_filter_profile()).categories)
        return [CategoryState(r["key"], r["key"] in selected, r["mode"]) for r in rows]

    async def set_category_enabled(self, key: str, enabled: bool) -> None:
        """Включить/выключить раздел: меняет профиль фильтров (источник правды) и старый флаг.

        Выключить последний выбранный раздел нельзя — ValueError, ничего не меняется.
        """
        async with self._tx() as db:
            cursor = await db.execute("SELECT data FROM filter_profile WHERE id = 1")
            row = await cursor.fetchone()
            profile = load_profile(row["data"] if row else None)
            keys = [k for k in profile.categories if k != key]
            if enabled:
                keys.append(key)
            if not keys:
                raise ValueError("Нужна хотя бы одна категория")
            updated = FilterProfile.model_validate({**profile.model_dump(), "categories": keys})
            await db.execute(
                "INSERT INTO filter_profile (id, data) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (dump_profile(updated),),
            )
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


async def backup_database(db_path: Path, *, now: datetime | None = None) -> Path:
    """Копия базы перед миграцией: `<папка базы>/backups/<имя>-<UTC-время>.db`.

    Используется встроенный механизм резервного копирования SQLite — копия целостная,
    даже если база в режиме WAL.
    """
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"{db_path.stem}-{stamp}.db"
    suffix = 1
    while target.exists():
        target = backup_dir / f"{db_path.stem}-{stamp}-{suffix}.db"
        suffix += 1
    await asyncio.to_thread(_copy_sqlite, db_path, target)
    log.info("копия базы перед миграцией: %s", target)
    return target


def _copy_sqlite(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
