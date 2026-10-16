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
from jobbot.models import Ad, AdDetails, Salary
from jobbot.normalize import normalize_city, normalize_text
from jobbot.parser import parse_salary
from jobbot.selection import CardFacts, StoredAd

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


async def _close_unwanted_pending(db: aiosqlite.Connection) -> None:
    """Объявления в очереди, которые больше никто не ждёт, — из очереди."""
    await db.execute(
        "UPDATE ads SET details_status = 'skipped' WHERE details_status = 'pending' "
        "AND ad_id NOT IN (SELECT ad_id FROM deliveries WHERE status = 'waiting')"
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


# Версия 3 (v1.2, друзья): пользователи, приглашения по @username, заявки, доставки.
# deliveries.chat_id = 0 — владелец (только ожидание страницы объявления; отправка владельцу
# по-прежнему через ads.matched/sent_at, как в v1.0).
_V3_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        chat_id   INTEGER PRIMARY KEY,
        username  TEXT,
        name      TEXT,
        status    TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'blocked')),
        can_edit  INTEGER NOT NULL DEFAULT 0,
        profile   TEXT NOT NULL,
        added_at  TEXT NOT NULL,
        paused_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
    """
    CREATE TABLE IF NOT EXISTS invites (
        username TEXT PRIMARY KEY,
        added_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS access_requests (
        chat_id      INTEGER PRIMARY KEY,
        username     TEXT,
        name         TEXT,
        status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'declined')),
        requested_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deliveries (
        ad_id      INTEGER NOT NULL,
        chat_id    INTEGER NOT NULL,
        status     TEXT NOT NULL CHECK (status IN ('ready', 'waiting', 'rejected')),
        created_at TEXT NOT NULL,
        sent_at    TEXT,
        PRIMARY KEY (ad_id, chat_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_deliveries_chat ON deliveries(chat_id, status, sent_at)",
)


async def _migration_v3(db: aiosqlite.Connection) -> None:
    for statement in _V3_SQL:
        await db.execute(statement)
    # Объявления, которые до v1.2 ждали страницу, ждали её для владельца.
    await db.execute(
        "INSERT OR IGNORE INTO deliveries (ad_id, chat_id, status, created_at) "
        "SELECT ad_id, 0, 'waiting', first_seen_at FROM ads WHERE details_status = 'pending'"
    )


# Миграции по порядку: номер версии = индекс + 1. Каждая выполняется в общей транзакции.
MIGRATIONS: tuple[Callable[[aiosqlite.Connection], Awaitable[None]], ...] = (
    _migration_v1,
    _migration_v2,
    _migration_v3,
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


OWNER_ID = 0  # chat_id владельца в таблице deliveries

UserStatus = Literal["active", "paused", "blocked"]


@dataclass(frozen=True, slots=True)
class User:
    """Друг, которому владелец открыл бота (v1.2)."""

    chat_id: int
    username: str | None
    name: str | None
    status: UserStatus
    can_edit: bool  # может сам менять свои фильтры (кроме категорий)
    profile: FilterProfile
    added_at: str
    paused_at: str | None = None

    @property
    def title(self) -> str:
        """Как показывать в меню: имя и @username."""
        parts = [self.name or ""] + ([f"@{self.username}"] if self.username else [])
        return " ".join(p for p in parts if p) or str(self.chat_id)


@dataclass(frozen=True, slots=True)
class AccessRequest:
    chat_id: int
    username: str | None
    name: str | None
    requested_at: str


def normalize_username(raw: str | None) -> str | None:
    """`@Ivan_Petrov` → `ivan_petrov`; пусто → None."""
    if not raw:
        return None
    value = raw.strip().lstrip("@").lower()
    return value or None


def _row_to_user(row: aiosqlite.Row) -> User:
    return User(
        chat_id=row["chat_id"],
        username=row["username"],
        name=row["name"],
        status=row["status"],
        can_edit=bool(row["can_edit"]),
        profile=load_profile(row["profile"]),
        added_at=row["added_at"],
        paused_at=row["paused_at"],
    )


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
            await db.execute(
                "UPDATE deliveries SET status = 'rejected' WHERE status = 'waiting' AND ad_id IN "
                "(SELECT ad_id FROM ads WHERE details_status = 'pending' AND first_seen_at < ?)",
                (to_iso(before),),
            )
            cursor = await db.execute(
                "UPDATE ads SET details_status = 'skipped' "
                "WHERE details_status = 'pending' AND first_seen_at < ?",
                (to_iso(before),),
            )
            return cursor.rowcount

    async def release_waiting(self, chat_id: int) -> int:
        """Получателю больше не нужны график/стаж: всё, что ждало страницу, подходит ему.

        Владельцу (chat_id = 0) ставится `matched = 1`, другу — доставка `ready`. Объявления,
        которые больше никто не ждёт, выходят из очереди (`skipped`).
        """
        async with self._tx() as db:
            if chat_id == OWNER_ID:
                await db.execute(
                    "UPDATE ads SET matched = 1 WHERE ad_id IN (SELECT ad_id FROM deliveries "
                    "WHERE chat_id = 0 AND status = 'waiting')"
                )
            cursor = await db.execute(
                "UPDATE deliveries SET status = 'ready' WHERE chat_id = ? AND status = 'waiting'",
                (chat_id,),
            )
            released = cursor.rowcount
            await _close_unwanted_pending(db)
            return released

    async def accept_pending_without_details(self) -> int:
        """Совместимость с v1.1: график/стаж сняты у владельца."""
        return await self.release_waiting(OWNER_ID)

    async def waiting_for(self, ad_id: int) -> list[int]:
        """Кто ждёт страницу этого объявления (0 — владелец)."""
        rows = await self._fetchall(
            "SELECT chat_id FROM deliveries WHERE ad_id = ? AND status = 'waiting'", (ad_id,)
        )
        return [r["chat_id"] for r in rows]

    async def resolve_details(
        self, ad_id: int, details: AdDetails, decisions: Mapping[int, bool]
    ) -> None:
        """Страница загружена: сохранить поля и решение для каждого, кто её ждал."""
        async with self._tx() as db:
            await db.execute(
                "UPDATE ads SET schedule = ?, experience = ?, company = ?, sphere = ?, "
                "city_norm = COALESCE(city_norm, ?), details_status = 'ok' WHERE ad_id = ?",
                (
                    details.schedule,
                    details.experience,
                    details.company,
                    details.sphere,
                    normalize_city(details.city) if details.city else None,
                    ad_id,
                ),
            )
            for chat_id, ok in decisions.items():
                if chat_id == OWNER_ID:
                    await db.execute("UPDATE ads SET matched = ? WHERE ad_id = ?", (int(ok), ad_id))
                await db.execute(
                    "UPDATE deliveries SET status = ? "
                    "WHERE ad_id = ? AND chat_id = ? AND status = 'waiting'",
                    ("ready" if ok else "rejected", ad_id, chat_id),
                )

    async def save_details(self, ad_id: int, details: AdDetails, *, matched: bool) -> None:
        """Совместимость с v1.1: решение только для владельца."""
        await self.resolve_details(ad_id, details, {OWNER_ID: matched})

    async def detail_failed(self, ad_id: int, *, permanent: bool = False) -> int:
        """Страница не загрузилась: +1 попытка; на 3-й (или сразу при `permanent`) — `failed`.

        `failed` → объявление подходит всем, кто его ждал, и уйдёт с пометкой «не проверено»:
        лучше лишнее уведомление, чем пропущенная вакансия. Возвращает число попыток.
        """
        async with self._tx() as db:
            cursor = await db.execute("SELECT details_attempts FROM ads WHERE ad_id = ?", (ad_id,))
            row = await cursor.fetchone()
            attempts = (row[0] if row else 0) + 1
            if permanent or attempts >= MAX_DETAIL_ATTEMPTS:
                cursor = await db.execute(
                    "SELECT 1 FROM deliveries "
                    "WHERE ad_id = ? AND chat_id = 0 AND status = 'waiting'",
                    (ad_id,),
                )
                owner_waits = await cursor.fetchone() is not None
                await db.execute(
                    "UPDATE ads SET details_attempts = ?, details_status = 'failed', "
                    "matched = CASE WHEN ? THEN 1 ELSE matched END WHERE ad_id = ?",
                    (attempts, int(owner_waits), ad_id),
                )
                await db.execute(
                    "UPDATE deliveries SET status = 'ready' WHERE ad_id = ? AND status = 'waiting'",
                    (ad_id,),
                )
            else:
                await db.execute(
                    "UPDATE ads SET details_attempts = ? WHERE ad_id = ?", (attempts, ad_id)
                )
            return attempts

    async def drop_pending_detail(self, ad_id: int) -> None:
        """Объявление удалено с сайта (404): не проверяем и не отправляем никому."""
        async with self._tx() as db:
            await db.execute(
                "UPDATE ads SET details_status = 'failed', matched = 0 WHERE ad_id = ?", (ad_id,)
            )
            await db.execute(
                "UPDATE deliveries SET status = 'rejected' WHERE ad_id = ? AND status = 'waiting'",
                (ad_id,),
            )

    # ---------- доставки друзьям (v1.2) ----------

    async def add_deliveries(
        self, rows: Iterable[tuple[int, int, str]], *, now: datetime | None = None
    ) -> None:
        """(ad_id, chat_id, 'ready'|'waiting') — кому подходит объявление; повтор не меняет."""
        stamp = to_iso(now)
        data = [(ad_id, chat_id, status, stamp) for ad_id, chat_id, status in rows]
        if not data:
            return
        async with self._tx() as db:
            await db.executemany(
                "INSERT OR IGNORE INTO deliveries (ad_id, chat_id, status, created_at) "
                "VALUES (?, ?, ?, ?)",
                data,
            )

    async def ready_for(self, chat_id: int, since: datetime) -> list[Ad]:
        """Подходящие другу и ещё не отправленные объявления, найденные не раньше `since`."""
        rows = await self._fetchall(
            "SELECT a.ad_id, a.category_key, a.title, a.url, a.salary_text, a.city, "
            "a.date_label, a.schedule, a.experience, a.company, a.sphere, a.details_status "
            "FROM deliveries d JOIN ads a ON a.ad_id = d.ad_id "
            "WHERE d.chat_id = ? AND d.status = 'ready' AND d.sent_at IS NULL "
            "AND a.first_seen_at >= ? ORDER BY a.first_seen_at, a.ad_id",
            (chat_id, to_iso(since)),
        )
        return [_row_to_ad(r) for r in rows]

    async def mark_delivered(
        self, chat_id: int, ad_ids: Iterable[int], *, now: datetime | None = None
    ) -> None:
        """Отметить доставку другу (если записи не было — создать уже отправленной)."""
        stamp = to_iso(now)
        rows = [(ad_id, chat_id, stamp, stamp) for ad_id in ad_ids]
        if not rows:
            return
        async with self._tx() as db:
            await db.executemany(
                "INSERT INTO deliveries (ad_id, chat_id, status, created_at, sent_at) "
                "VALUES (?, ?, 'ready', ?, ?) "
                "ON CONFLICT(ad_id, chat_id) DO UPDATE SET status = 'ready', "
                "sent_at = excluded.sent_at",
                rows,
            )

    async def count_delivered_since(self, chat_id: int, since: datetime) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) FROM deliveries WHERE chat_id = ? AND sent_at >= ?",
            (chat_id, to_iso(since)),
        )
        return int(row[0]) if row else 0

    # ---------- друзья (v1.2) ----------

    async def list_users(self) -> list[User]:
        rows = await self._fetchall("SELECT * FROM users ORDER BY added_at, chat_id")
        return [_row_to_user(r) for r in rows]

    async def get_user(self, chat_id: int) -> User | None:
        row = await self._fetchone("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
        return _row_to_user(row) if row else None

    async def find_user_by_username(self, username: str) -> User | None:
        row = await self._fetchone(
            "SELECT * FROM users WHERE username = ?", (normalize_username(username),)
        )
        return _row_to_user(row) if row else None

    async def add_user(
        self,
        chat_id: int,
        *,
        username: str | None = None,
        name: str | None = None,
        profile: FilterProfile | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Открыть доступ. False — пользователь уже есть (ничего не меняется)."""
        async with self._tx() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO users (chat_id, username, name, profile, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    chat_id,
                    normalize_username(username),
                    (name or "")[:64] or None,
                    dump_profile(profile or FilterProfile()),
                    to_iso(now),
                ),
            )
            added = cursor.rowcount == 1
            if added:
                await db.execute("DELETE FROM access_requests WHERE chat_id = ?", (chat_id,))
                if username:
                    await db.execute(
                        "DELETE FROM invites WHERE username = ?", (normalize_username(username),)
                    )
            return added

    async def update_user_identity(
        self, chat_id: int, username: str | None, name: str | None
    ) -> None:
        """Обновить @username и имя (они могут меняться в Telegram)."""
        async with self._tx() as db:
            await db.execute(
                "UPDATE users SET username = ?, name = ? WHERE chat_id = ?",
                (normalize_username(username), (name or "")[:64] or None, chat_id),
            )

    async def set_user_status(
        self, chat_id: int, status: UserStatus, *, now: datetime | None = None
    ) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE users SET status = ?, paused_at = ? WHERE chat_id = ?",
                (status, to_iso(now) if status == "paused" else None, chat_id),
            )

    async def set_user_can_edit(self, chat_id: int, can_edit: bool) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE users SET can_edit = ? WHERE chat_id = ?", (int(can_edit), chat_id)
            )

    async def delete_user(self, chat_id: int) -> None:
        async with self._tx() as db:
            await db.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
            await db.execute("DELETE FROM deliveries WHERE chat_id = ?", (chat_id,))

    async def get_user_profile(self, chat_id: int) -> FilterProfile:
        user = await self.get_user(chat_id)
        return user.profile if user else FilterProfile()

    async def update_user_profile(
        self, chat_id: int, mutator: Callable[[FilterProfile], FilterProfile | None]
    ) -> FilterProfile:
        """Как `update_profile`, но для друга. Нет такого пользователя → KeyError."""
        async with self._tx() as db:
            cursor = await db.execute("SELECT profile FROM users WHERE chat_id = ?", (chat_id,))
            row = await cursor.fetchone()
            if row is None:
                raise KeyError(chat_id)
            draft = load_profile(row["profile"]).model_copy(deep=True)
            changed = mutator(draft) or draft
            profile = FilterProfile.model_validate(changed.model_dump())
            await db.execute(
                "UPDATE users SET profile = ? WHERE chat_id = ?", (dump_profile(profile), chat_id)
            )
        return profile

    # ---------- приглашения по @username и заявки ----------

    async def add_invite(self, username: str, *, now: datetime | None = None) -> bool:
        value = normalize_username(username)
        if not value:
            return False
        async with self._tx() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO invites (username, added_at) VALUES (?, ?)",
                (value, to_iso(now)),
            )
            return cursor.rowcount == 1

    async def list_invites(self) -> list[str]:
        rows = await self._fetchall("SELECT username FROM invites ORDER BY added_at, username")
        return [r["username"] for r in rows]

    async def has_invite(self, username: str | None) -> bool:
        value = normalize_username(username)
        if not value:
            return False
        return (
            await self._fetchone("SELECT 1 FROM invites WHERE username = ?", (value,)) is not None
        )

    async def delete_invite(self, username: str) -> None:
        async with self._tx() as db:
            await db.execute(
                "DELETE FROM invites WHERE username = ?", (normalize_username(username),)
            )

    async def record_request(
        self, chat_id: int, username: str | None, name: str | None, *, now: datetime | None = None
    ) -> bool:
        """Незнакомец написал боту. True — заявка новая (владельцу сообщить один раз)."""
        async with self._tx() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO access_requests (chat_id, username, name, requested_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, normalize_username(username), (name or "")[:64] or None, to_iso(now)),
            )
            return cursor.rowcount == 1

    async def list_requests(self) -> list[AccessRequest]:
        rows = await self._fetchall(
            "SELECT chat_id, username, name, requested_at FROM access_requests "
            "WHERE status = 'pending' ORDER BY requested_at DESC"
        )
        return [
            AccessRequest(r["chat_id"], r["username"], r["name"], r["requested_at"]) for r in rows
        ]

    async def get_request(self, chat_id: int) -> AccessRequest | None:
        row = await self._fetchone(
            "SELECT chat_id, username, name, requested_at FROM access_requests "
            "WHERE chat_id = ? AND status = 'pending'",
            (chat_id,),
        )
        return (
            AccessRequest(row["chat_id"], row["username"], row["name"], row["requested_at"])
            if row
            else None
        )

    async def find_request_by_username(self, username: str) -> AccessRequest | None:
        row = await self._fetchone(
            "SELECT chat_id, username, name, requested_at FROM access_requests "
            "WHERE username = ? AND status = 'pending'",
            (normalize_username(username),),
        )
        return (
            AccessRequest(row["chat_id"], row["username"], row["name"], row["requested_at"])
            if row
            else None
        )

    async def decline_request(self, chat_id: int) -> None:
        """Отклонить: больше не беспокоить владельца этим человеком."""
        async with self._tx() as db:
            await db.execute(
                "UPDATE access_requests SET status = 'declined' WHERE chat_id = ?", (chat_id,)
            )

    async def ads_since(self, since: datetime) -> list[StoredAd]:
        """Все объявления, найденные не раньше `since` (новые первыми), с разобранной карточкой."""
        rows = await self._fetchall(
            "SELECT ad_id, category_key, title, url, salary_text, city, date_label, schedule, "
            "experience, company, sphere, details_status, salary_min, salary_max, "
            "salary_negotiable, city_norm FROM ads WHERE first_seen_at >= ? "
            "ORDER BY first_seen_at DESC, ad_id DESC",
            (to_iso(since),),
        )
        result = []
        for row in rows:
            ad = _row_to_ad(row)
            salary = Salary(row["salary_min"], row["salary_max"], bool(row["salary_negotiable"]))
            facts = CardFacts(ad.category_key, ad.title, row["city_norm"], salary)
            result.append(StoredAd(ad, facts))
        return result

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

    async def remove_keyword_from(self, kind: KeywordKind, word: str) -> bool:
        """Удалить слово только из одного списка (кнопка в меню). False — слова не было."""
        async with self._tx() as db:
            cursor = await db.execute(
                "DELETE FROM keywords WHERE kind = ? AND word = ?", (kind, normalize(word))
            )
            return cursor.rowcount == 1

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


@dataclass(frozen=True, slots=True)
class FiltersSnapshot:
    """Фильтры из базы для режима `--dry-run` (база только читается)."""

    profile: FilterProfile
    include: list[str]
    exclude: list[str]
    modes: dict[str, str]


def read_filters_readonly(db_path: str | Path) -> FiltersSnapshot | None:
    """Прочитать профиль, слова и режимы, ничего не меняя в базе. Нет базы → None."""
    path = Path(db_path)
    if not path.is_file():
        return None
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        raw = None
        if "filter_profile" in tables:
            row = conn.execute("SELECT data FROM filter_profile WHERE id = 1").fetchone()
            raw = row[0] if row else None
        words: dict[str, list[str]] = {"include": [], "exclude": []}
        if "keywords" in tables:
            for kind, word in conn.execute("SELECT kind, word FROM keywords ORDER BY word"):
                words[kind].append(word)
        modes = {}
        if "categories" in tables:
            modes = dict(conn.execute("SELECT key, mode FROM categories"))
    return FiltersSnapshot(load_profile(raw), words["include"], words["exclude"], modes)


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
