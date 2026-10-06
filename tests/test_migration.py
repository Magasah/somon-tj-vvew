"""Миграция базы v1 → v2 (v1.1): данные не теряются, профиль создаётся из старых категорий,
перед миграцией делается копия, повторный запуск ничего не меняет."""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from jobbot.filters import FilterProfile
from jobbot.storage import _V1_SQL, SCHEMA_VERSION, Storage


def make_v1_database(path: Path, *, enabled: tuple[str, ...] = ("it",)) -> None:
    """База в том виде, в каком её оставляет бот версии 1.0 (с данными)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        for statement in _V1_SQL:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.executemany(
            "INSERT INTO ads (ad_id, category_key, title, url, salary_text, city, date_label, "
            "matched, first_seen_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "it", "Python", "https://somon.tj/adv/1_x/", "5 000 c.", "Душанбе",
                 "Сегодня", 1, "2026-10-05T10:00:00Z", "2026-10-05T10:00:05Z"),
                (2, "students", "Стажёр", "https://somon.tj/adv/2_x/", "Договорная", "Худжанд",
                 "Вчера", 0, "2026-10-05T10:00:00Z", "0000-00-00T00:00:00Z"),
                (3, "it", "Backend", "https://somon.tj/adv/3_x/", None, None,
                 None, 1, "2026-10-05T11:00:00Z", None),
            ],
        )  # fmt: skip
        conn.executemany(
            "INSERT INTO keywords (kind, word) VALUES (?, ?)",
            [("include", "python"), ("exclude", "колл-центр")],
        )
        conn.executemany(
            "INSERT INTO categories (key, enabled, mode) VALUES (?, ?, ?)",
            [(key, int(key in enabled), "all") for key in ("it", "students")],
        )
        conn.executemany(
            "INSERT INTO state (key, value) VALUES (?, ?)",
            [("paused", "0"), ("defaults_seeded", "1"), ("last_poll_at", "2026-10-05T11:00:00Z")],
        )


def table_dump(path: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(sql).fetchall()


OLD_ADS_SQL = (
    "SELECT ad_id, category_key, title, url, salary_text, city, date_label, matched, "
    "first_seen_at, sent_at FROM ads ORDER BY ad_id"
)


async def test_v1_to_v2_keeps_data_and_creates_profile(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "jobbot.db"
    make_v1_database(db_path, enabled=("it",))
    ads_before = table_dump(db_path, OLD_ADS_SQL)
    keywords_before = table_dump(db_path, "SELECT kind, word FROM keywords ORDER BY kind, word")
    state_before = table_dump(db_path, "SELECT key, value FROM state ORDER BY key")

    store = await Storage.open(db_path)
    try:
        assert await store.schema_version() == SCHEMA_VERSION == 2
        profile = await store.get_filter_profile()
        assert profile.categories == ["it"]  # из включённых разделов v1
        assert profile == FilterProfile(categories=["it"])  # остальное — по умолчанию
        # старые функции работают на мигрированной базе
        assert await store.count_ads() == 3
        assert await store.get_keywords("include") == ["python"]
        assert [a.ad_id for a in await store.recent_matched(10)] == [3, 1]
    finally:
        await store.close()

    assert table_dump(db_path, OLD_ADS_SQL) == ads_before
    assert table_dump(db_path, "SELECT kind, word FROM keywords ORDER BY kind, word") == (
        keywords_before
    )
    assert table_dump(db_path, "SELECT key, value FROM state ORDER BY key") == state_before
    # зарплата и город старых объявлений разобраны, остальное — значения по умолчанию
    assert table_dump(
        db_path,
        "SELECT ad_id, salary_min, salary_max, salary_negotiable, city_norm, schedule, "
        "experience, company, sphere, details_status, details_attempts FROM ads ORDER BY ad_id",
    ) == [
        (1, 5000, 5000, 0, "душанбе", None, None, None, None, "none", 0),
        (2, None, None, 1, "худжанд", None, None, None, None, "none", 0),
        (3, None, None, 0, None, None, None, None, None, "none", 0),
    ]
    tables = {r[0] for r in table_dump(db_path, "SELECT name FROM sqlite_master")}
    assert {"filter_profile", "attr_values", "idx_ads_details"} <= tables


async def test_backup_is_made_before_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "jobbot.db"
    make_v1_database(db_path)
    ads_before = table_dump(db_path, OLD_ADS_SQL)

    store = await Storage.open(db_path)
    await store.close()

    backups = sorted((db_path.parent / "backups").glob("jobbot-*.db"))
    assert len(backups) == 1
    assert table_dump(backups[0], "SELECT MAX(version) FROM schema_version") == [(1,)]
    assert table_dump(backups[0], OLD_ADS_SQL) == ads_before


async def test_second_open_changes_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "jobbot.db"
    make_v1_database(db_path, enabled=("it", "students"))
    store = await Storage.open(db_path)
    await store.close()
    snapshot = table_dump(db_path, "SELECT * FROM ads ORDER BY ad_id")
    profile_row = table_dump(db_path, "SELECT data FROM filter_profile")

    # Владелец успел поменять старые настройки — профиль при повторном запуске не перезаписывается
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE categories SET enabled = 0 WHERE key = 'students'")

    store = await Storage.open(db_path)
    try:
        assert await store.schema_version() == 2
        assert (await store.get_filter_profile()).categories == ["it", "students"]
    finally:
        await store.close()
    assert table_dump(db_path, "SELECT * FROM ads ORDER BY ad_id") == snapshot
    assert table_dump(db_path, "SELECT data FROM filter_profile") == profile_row
    assert len(list((db_path.parent / "backups").glob("*.db"))) == 1  # вторая копия не нужна


async def test_migration_survives_partially_added_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "jobbot.db"
    make_v1_database(db_path)
    with sqlite3.connect(db_path) as conn:  # как будто часть столбцов уже добавлена вручную
        conn.execute("ALTER TABLE ads ADD COLUMN salary_min INTEGER")
        conn.execute("ALTER TABLE ads ADD COLUMN city_norm TEXT")
    store = await Storage.open(db_path)
    try:
        assert await store.schema_version() == 2
    finally:
        await store.close()


async def test_all_categories_disabled_in_v1_gives_default_profile(tmp_path: Path) -> None:
    db_path = tmp_path / "jobbot.db"
    make_v1_database(db_path, enabled=())
    store = await Storage.open(db_path)
    try:
        assert (await store.get_filter_profile()).categories == ["it", "students"]
    finally:
        await store.close()


async def test_fresh_database_gets_default_profile_and_no_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "jobbot.db"
    store = await Storage.open(db_path)
    try:
        assert await store.schema_version() == 2
        assert await store.get_filter_profile() == FilterProfile()
    finally:
        await store.close()
    assert not (db_path.parent / "backups").exists()


# ---------- профиль в базе ----------


async def test_broken_profile_in_db_falls_back_to_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "jobbot.db"
    store = await Storage.open(db_path)
    await store.close()
    for bad in ("{не json", '{"categories": ["nope"]}', '{"salary": {"min": 9, "max": 1}}'):
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE filter_profile SET data = ? WHERE id = 1", (bad,))
        store = await Storage.open(db_path)
        try:
            with caplog.at_level("WARNING", logger="filters"):
                assert await store.get_filter_profile() == FilterProfile()
        finally:
            await store.close()
    assert "повреждён" in caplog.text


async def test_update_profile_validates_and_rolls_back(tmp_path: Path) -> None:
    from pydantic import ValidationError

    store = await Storage.open(tmp_path / "jobbot.db")
    try:

        def set_cities(profile: FilterProfile) -> None:
            profile.cities = ["душанбе", "худжанд"]

        assert (await store.update_profile(set_cities)).cities == ["душанбе", "худжанд"]

        def break_it(profile: FilterProfile) -> None:
            profile.categories = []  # последнюю категорию снять нельзя

        with pytest.raises(ValidationError):
            await store.update_profile(break_it)
        assert (await store.get_filter_profile()).cities == ["душанбе", "худжанд"]
        assert (await store.get_filter_profile()).categories == ["it", "students"]
    finally:
        await store.close()


async def test_update_profile_concurrent_changes_are_not_lost(tmp_path: Path) -> None:
    store = await Storage.open(tmp_path / "jobbot.db")
    try:

        def add_city(name: str):
            def mutate(profile: FilterProfile) -> None:
                profile.cities.append(name)

            return mutate

        names = [f"город {i}" for i in range(15)]
        await asyncio.gather(*(store.update_profile(add_city(n)) for n in names))
        assert sorted((await store.get_filter_profile()).cities) == sorted(names)
    finally:
        await store.close()
