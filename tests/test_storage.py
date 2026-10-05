"""Тесты хранилища: схема, миграции, дубли, отправка, слова, разделы, состояние."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobbot.config import DEFAULT_CATEGORIES, DEFAULT_EXCLUDE, DEFAULT_INCLUDE
from jobbot.models import Ad
from jobbot.storage import SCHEMA_VERSION, Storage

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def make_ad(ad_id: int, category: str = "it", title: str = "Python разработчик") -> Ad:
    return Ad(
        ad_id=ad_id,
        url=f"https://somon.tj/adv/{ad_id}_x/",
        title=title,
        category_key=category,
        salary_text="5 000 c.",
        city="Душанбе",
        date_label="Сегодня",
    )


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    store = await Storage.open(tmp_path / "data" / "jobbot.db")
    yield store
    await store.close()


async def test_open_creates_directory_schema_and_wal(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "jobbot.db"
    store = await Storage.open(path)
    try:
        assert await store.schema_version() == SCHEMA_VERSION
    finally:
        await store.close()
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"schema_version", "ads", "keywords", "categories", "state"} <= tables
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_ads_category", "idx_ads_seen"} <= indexes
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


async def test_reopen_is_idempotent_and_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "jobbot.db"
    store = await Storage.open(path)
    await store.add_ads([(make_ad(1), True)], now=NOW)
    await store.close()

    store = await Storage.open(path)
    try:
        assert await store.count_ads() == 1
        assert await store.schema_version() == SCHEMA_VERSION
    finally:
        await store.close()


async def test_database_from_newer_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "jobbot.db"
    store = await Storage.open(path)
    await store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 1,))
    with pytest.raises(RuntimeError, match="новой версией"):
        await Storage.open(path)


async def test_same_ad_id_is_not_duplicated(storage: Storage) -> None:
    first = await storage.add_ads([(make_ad(1), True)], now=NOW)
    again = await storage.add_ads([(make_ad(1, title="Другой заголовок"), True)], now=NOW)
    assert [a.ad_id for a in first] == [1]
    assert again == []
    assert await storage.count_ads() == 1
    # старая запись не перезаписана
    assert (await storage.recent_matched(10))[0].title == "Python разработчик"


async def test_duplicate_inside_one_batch(storage: Storage) -> None:
    new = await storage.add_ads([(make_ad(1), True), (make_ad(1), True), (make_ad(2), False)])
    assert [a.ad_id for a in new] == [1, 2]
    assert await storage.count_ads() == 2


async def test_unmatched_ads_are_saved_but_never_unsent(storage: Storage) -> None:
    await storage.add_ads([(make_ad(1), False)], now=NOW)
    assert await storage.count_ads() == 1
    assert await storage.unsent_matched(NOW - timedelta(hours=1)) == []
    assert await storage.recent_matched(10) == []


async def test_unsent_then_mark_sent(storage: Storage) -> None:
    await storage.add_ads([(make_ad(1), True), (make_ad(2), True)], now=NOW)
    unsent = await storage.unsent_matched(NOW - timedelta(hours=24))
    assert [a.ad_id for a in unsent] == [1, 2]

    await storage.mark_sent([1], now=NOW)
    assert [a.ad_id for a in await storage.unsent_matched(NOW - timedelta(hours=24))] == [2]
    assert await storage.count_sent_since(NOW - timedelta(minutes=1)) == 1


async def test_seeded_ads_saved_as_sent(storage: Storage) -> None:
    await storage.add_ads([(make_ad(1), True)], skip_sending=True, now=NOW)
    assert await storage.unsent_matched(NOW - timedelta(days=1)) == []
    # но в /last они видны
    assert [a.ad_id for a in await storage.recent_matched(5)] == [1]


async def test_unsent_older_than_window_is_ignored(storage: Storage) -> None:
    await storage.add_ads([(make_ad(1), True)], now=NOW - timedelta(hours=30))
    await storage.add_ads([(make_ad(2), True)], now=NOW - timedelta(hours=2))
    unsent = await storage.unsent_matched(NOW - timedelta(hours=24))
    assert [a.ad_id for a in unsent] == [2]


async def test_has_ads_by_category(storage: Storage) -> None:
    assert not await storage.has_ads("it")
    await storage.add_ads([(make_ad(1, "it"), False)])
    assert await storage.has_ads("it")
    assert not await storage.has_ads("students")


async def test_recent_matched_order_limit_and_category(storage: Storage) -> None:
    for i in range(1, 4):
        await storage.add_ads([(make_ad(i, "it"), True)], now=NOW + timedelta(minutes=i))
    await storage.add_ads([(make_ad(10, "students"), True)], now=NOW + timedelta(minutes=10))
    assert [a.ad_id for a in await storage.recent_matched(2)] == [10, 3]
    assert [a.ad_id for a in await storage.recent_matched(10, "it")] == [3, 2, 1]


async def test_ad_roundtrip_keeps_fields(storage: Storage) -> None:
    ad = Ad(5, "https://somon.tj/adv/5_x/", "T", "it", None, None, None)
    await storage.add_ads([(ad, True)])
    assert (await storage.recent_matched(1))[0] == ad


async def test_failed_batch_is_rolled_back(storage: Storage) -> None:
    class Boom:
        ad_id = 2  # нет остальных полей Ad → ошибка при вставке второго элемента

    with pytest.raises(AttributeError):
        await storage.add_ads([(make_ad(1), True), (Boom(), True)])  # type: ignore[list-item]
    assert await storage.count_ads() == 0  # первая вставка откатилась
    # и хранилище после ошибки работает
    await storage.add_ads([(make_ad(3), True)])
    assert await storage.count_ads() == 1


async def test_concurrent_writes_do_not_break(storage: Storage) -> None:
    results = await asyncio.gather(
        *(storage.add_ads([(make_ad(i), True)]) for i in range(1, 21)),
        storage.set_state("x", "1"),
    )
    assert sum(len(r) for r in results[:-1]) == 20
    assert await storage.count_ads() == 20


async def test_keywords_normalized_and_deduplicated(storage: Storage) -> None:
    assert await storage.add_keyword("include", "  Стажёр ")
    assert not await storage.add_keyword("include", "стажер")
    assert await storage.add_keyword("exclude", "стажер")  # в другом списке — отдельная запись
    assert await storage.get_keywords("include") == ["стажер"]
    assert await storage.count_keywords("exclude") == 1

    assert await storage.remove_keyword("СТАЖЁР") == 2  # из обоих списков
    assert await storage.get_keywords("include") == []
    assert await storage.remove_keyword("нет такого") == 0


async def test_seed_defaults(storage: Storage) -> None:
    await storage.seed_defaults(DEFAULT_CATEGORIES, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    assert sorted(await storage.get_keywords("include")) == sorted(DEFAULT_INCLUDE)
    assert sorted(await storage.get_keywords("exclude")) == sorted(DEFAULT_EXCLUDE)
    cats = await storage.get_categories()
    assert [(c.key, c.enabled, c.mode) for c in cats] == [
        ("it", True, "all"),
        ("students", True, "all"),
    ]


async def test_seed_defaults_does_not_resurrect_removed_words_or_reset_categories(
    storage: Storage,
) -> None:
    await storage.seed_defaults(DEFAULT_CATEGORIES, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    await storage.remove_keyword("python")
    await storage.set_category_enabled("it", False)
    await storage.set_category_mode("students", "keywords")

    await storage.seed_defaults(
        DEFAULT_CATEGORIES, DEFAULT_INCLUDE, DEFAULT_EXCLUDE
    )  # «перезапуск»
    assert "python" not in await storage.get_keywords("include")
    cats = {c.key: c for c in await storage.get_categories()}
    assert not cats["it"].enabled
    assert cats["students"].mode == "keywords"


async def test_new_category_in_config_is_added_on_restart(storage: Storage) -> None:
    await storage.seed_defaults(DEFAULT_CATEGORIES[:1], DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    await storage.seed_defaults(DEFAULT_CATEGORIES, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    assert [c.key for c in await storage.get_categories()] == ["it", "students"]


async def test_state_get_set_delete(storage: Storage) -> None:
    assert await storage.get_state("paused") is None
    assert await storage.get_state("paused", "0") == "0"
    await storage.set_state("paused", "1")
    await storage.set_state("paused", "0")  # обновление существующего ключа
    assert await storage.get_state("paused") == "0"
    await storage.delete_state("paused")
    assert await storage.get_state("paused") is None


async def test_sql_injection_is_just_text(storage: Storage) -> None:
    evil = "x'); DROP TABLE ads; --"
    await storage.add_keyword("include", evil)
    await storage.add_ads([(make_ad(1, title=evil), True)])
    assert await storage.count_ads() == 1
    assert (await storage.recent_matched(1))[0].title == evil
