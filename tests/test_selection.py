"""Отбор v2 (раздел 6 TZ_update_filters.md): каждый фильтр отдельно, комбинации, справочник."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobbot.filters import FilterProfile, SalaryFilter
from jobbot.models import Ad, AdDetails, Salary
from jobbot.selection import (
    CardFacts,
    Decision,
    check_card,
    check_details,
    initial_details_status,
    salary_ok,
)
from jobbot.storage import Storage


def facts(
    *,
    category: str = "it",
    title: str = "Python разработчик",
    city: str | None = "душанбе",
    salary: Salary = Salary(3000, 3000),  # noqa: B008 — неизменяемый dataclass
) -> CardFacts:
    return CardFacts(category, title, city, salary)


def decide(card: CardFacts, profile: FilterProfile, **kw: object) -> Decision:
    options: dict[str, object] = {"keyword_mode": "all", "include": [], "exclude": []}
    options.update(kw)
    return check_card(card, profile, **options)  # type: ignore[arg-type]


# ---------- по умолчанию ----------


def test_default_profile_accepts_like_v1() -> None:
    assert decide(facts(), FilterProfile()) is Decision.ACCEPT
    assert decide(facts(city=None, salary=Salary()), FilterProfile()) is Decision.ACCEPT


def test_card_facts_from_ad() -> None:
    ad = Ad(1, "https://somon.tj/adv/1_x/", "Курьер", "students", "VIP900 c.", "Куляб", "Сегодня")
    assert CardFacts.from_ad(ad) == CardFacts("students", "Курьер", "кулоб", Salary(900, 900))


# ---------- категория ----------


def test_category_filter() -> None:
    profile = FilterProfile(categories=["it"])
    assert decide(facts(category="it"), profile) is Decision.ACCEPT
    assert decide(facts(category="students"), profile) is Decision.REJECT


# ---------- город ----------


def test_city_filter() -> None:
    profile = FilterProfile(cities=["душанбе", "кулоб"])
    assert decide(facts(city="душанбе"), profile) is Decision.ACCEPT
    assert decide(facts(city="кулоб"), profile) is Decision.ACCEPT
    assert decide(facts(city="худжанд"), profile) is Decision.REJECT


def test_city_filter_uses_synonyms_on_profile_side() -> None:
    # даже если в профиль попало «Куляб» как есть — совпадёт с нормализованным городом
    assert decide(facts(city="кулоб"), FilterProfile(cities=["Куляб"])) is Decision.ACCEPT


@pytest.mark.parametrize(
    ("unknown", "expected"), [(True, Decision.ACCEPT), (False, Decision.REJECT)]
)
def test_unknown_city_follows_include_unknown(unknown: bool, expected: Decision) -> None:
    profile = FilterProfile(cities=["душанбе"], include_unknown_attrs=unknown)
    assert decide(facts(city=None), profile) is expected


def test_empty_cities_means_all_even_if_unknown_hidden() -> None:
    profile = FilterProfile(include_unknown_attrs=False)
    assert decide(facts(city=None), profile) is Decision.ACCEPT


# ---------- зарплата ----------


@pytest.mark.parametrize(
    ("ad", "wanted", "expected"),
    [
        # пересечение диапазонов (раздел 6, п. 3): ≥ 6 случаев
        (Salary(3000, 3000), SalaryFilter(min=2000), True),  # выше «от»
        (Salary(1500, 1500), SalaryFilter(min=2000), False),  # ниже «от»
        (Salary(2000, 2000), SalaryFilter(min=2000), True),  # ровно на границе
        (Salary(1500, 3000), SalaryFilter(min=2000), True),  # диапазон задевает «от»
        (Salary(1000, 1999), SalaryFilter(min=2000), False),  # диапазон целиком ниже
        (Salary(6000, 8000), SalaryFilter(max=5000), False),  # целиком выше «до»
        (Salary(4000, 8000), SalaryFilter(max=5000), True),  # задевает «до»
        (Salary(2500, 2500), SalaryFilter(min=2000, max=3000), True),  # внутри окна
        (Salary(1000, 9000), SalaryFilter(min=2000, max=3000), True),  # накрывает окно
        (Salary(3001, 4000), SalaryFilter(min=2000, max=3000), False),  # правее окна
        (Salary(3000, None), SalaryFilter(max=2500), False),  # «от 3000»: край = 3000
        (Salary(3000, None), SalaryFilter(min=2000), True),
        (Salary(None, 1500), SalaryFilter(min=2000), False),  # «до 1500»: край = 1500
        (Salary(None, 2500), SalaryFilter(min=2000), True),
        (Salary(5000, 5000), SalaryFilter(), True),  # фильтр не задан
    ],
)
def test_salary_ranges(ad: Salary, wanted: SalaryFilter, expected: bool) -> None:
    assert salary_ok(ad, wanted, include_unknown=True) is expected


@pytest.mark.parametrize(
    ("show_negotiable", "limits", "expected"),
    [
        (True, {}, True),
        (False, {}, False),
        (True, {"min": 5000}, True),  # «Договорная» не сравнивается с границами
        (False, {"min": 5000}, False),
    ],
)
def test_negotiable(show_negotiable: bool, limits: dict, expected: bool) -> None:
    wanted = SalaryFilter(include_negotiable=show_negotiable, **limits)
    assert salary_ok(Salary(negotiable=True), wanted, include_unknown=True) is expected


@pytest.mark.parametrize(
    ("limits", "unknown", "expected"),
    [
        ({"min": 2000}, True, True),
        ({"min": 2000}, False, False),
        ({}, False, True),  # без границ неизвестная зарплата не мешает
    ],
)
def test_unknown_salary(limits: dict, unknown: bool, expected: bool) -> None:
    assert salary_ok(Salary(), SalaryFilter(**limits), include_unknown=unknown) is expected


def test_salary_filter_in_check_card() -> None:
    profile = FilterProfile(salary=SalaryFilter(min=2000, include_negotiable=False))
    assert decide(facts(salary=Salary(2500, 2500)), profile) is Decision.ACCEPT
    assert decide(facts(salary=Salary(900, 900)), profile) is Decision.REJECT
    assert decide(facts(salary=Salary(negotiable=True)), profile) is Decision.REJECT


# ---------- ключевые слова (как в v1.0) ----------


def test_keywords_work_as_before() -> None:
    profile = FilterProfile()
    exclude = ["колл-центр"]
    assert decide(facts(title="Оператор колл-центра"), profile, exclude=exclude) is Decision.REJECT
    kw = {"keyword_mode": "keywords", "include": ["python"], "exclude": exclude}
    assert decide(facts(title="Python разработчик"), profile, **kw) is Decision.ACCEPT
    assert decide(facts(title="Водитель"), profile, **kw) is Decision.REJECT


# ---------- ступень 2: график и стаж ----------


def test_details_not_needed_without_schedule_or_experience() -> None:
    profile = FilterProfile(cities=["душанбе"], salary=SalaryFilter(min=1000))
    decision = decide(facts(), profile)
    assert decision is Decision.ACCEPT
    assert initial_details_status(decision) == "skipped"


@pytest.mark.parametrize(
    "profile", [FilterProfile(schedules=["полный день"]), FilterProfile(experience=["без опыта"])]
)
def test_details_needed_when_schedule_or_experience_selected(profile: FilterProfile) -> None:
    decision = decide(facts(), profile)
    assert decision is Decision.NEED_DETAILS
    assert initial_details_status(decision) == "pending"


def test_card_rejected_before_details() -> None:
    profile = FilterProfile(categories=["it"], schedules=["полный день"])
    decision = decide(facts(category="students"), profile)
    assert decision is Decision.REJECT
    assert initial_details_status(decision) == "skipped"  # страницу не открываем


def test_schedule_filter() -> None:
    profile = FilterProfile(schedules=["полный день", "удаленно"])
    assert check_details(AdDetails(schedule="Полный день"), profile)
    assert check_details(AdDetails(schedule="  УДАЛЕННО "), profile)
    assert not check_details(AdDetails(schedule="Посменно"), profile)


def test_experience_filter() -> None:
    profile = FilterProfile(experience=["без опыта", "от 1 года"])
    assert check_details(AdDetails(experience="Без опыта"), profile)
    assert check_details(AdDetails(experience="От 1 года"), profile)
    assert not check_details(AdDetails(experience="От 3 лет"), profile)


@pytest.mark.parametrize(("unknown", "expected"), [(True, True), (False, False)])
def test_unknown_details_follow_include_unknown(unknown: bool, expected: bool) -> None:
    profile = FilterProfile(
        schedules=["полный день"], experience=["без опыта"], include_unknown_attrs=unknown
    )
    assert check_details(AdDetails(), profile) is expected
    assert check_details(AdDetails(schedule="Полный день"), profile) is expected


def test_details_any_when_not_selected() -> None:
    assert check_details(AdDetails(schedule="Ночной", experience="От 3 лет"), FilterProfile())


# ---------- комбинация ----------


def test_combination_of_all_filters() -> None:
    profile = FilterProfile(
        categories=["it"],
        cities=["душанбе"],
        salary=SalaryFilter(min=2000, include_negotiable=False),
        schedules=["полный день"],
        experience=["без опыта"],
        include_unknown_attrs=False,
    )
    good = facts(category="it", city="душанбе", salary=Salary(2000, 3000))
    assert decide(good, profile) is Decision.NEED_DETAILS
    assert check_details(AdDetails(schedule="Полный день", experience="Без опыта"), profile)
    # каждое отдельное несовпадение отклоняет
    assert decide(facts(category="students", salary=Salary(3000, 3000)), profile) is Decision.REJECT
    assert decide(facts(city="худжанд", salary=Salary(3000, 3000)), profile) is Decision.REJECT
    assert decide(facts(salary=Salary(1000, 1000)), profile) is Decision.REJECT
    assert decide(facts(salary=Salary(negotiable=True)), profile) is Decision.REJECT
    assert decide(facts(city=None, salary=Salary(3000, 3000)), profile) is Decision.REJECT
    assert not check_details(AdDetails(schedule="Посменно", experience="Без опыта"), profile)
    assert not check_details(AdDetails(schedule="Полный день", experience="Любой"), profile)
    assert not check_details(AdDetails(schedule="Полный день"), profile)  # стаж не указан


# ---------- справочник attr_values ----------


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    store = await Storage.open(tmp_path / "jobbot.db")
    yield store
    await store.close()


async def test_attr_values_upsert_and_order(storage: Storage) -> None:
    t0 = datetime(2026, 10, 6, 10, tzinfo=UTC)
    await storage.record_attr_values("city", ["Душанбе", "Худжанд", "Куляб"], now=t0)
    await storage.record_attr_values("city", ["Душанбе", "Кӯлоб", None, ""], now=t0)
    await storage.record_attr_values("city", ["  душанбе "], now=t0 + timedelta(hours=1))

    values = await storage.get_attr_values("city")
    assert [(v.value_norm, v.label, v.seen_count) for v in values] == [
        ("душанбе", "Душанбе", 3),
        ("кулоб", "Куляб", 2),  # «Куляб» и «Кӯлоб» — одно значение, подпись первая
        ("худжанд", "Худжанд", 1),
    ]
    assert await storage.get_attr_values("city", limit=1) == values[:1]


async def test_attr_values_are_separate_per_attr(storage: Storage) -> None:
    await storage.record_attr_values("schedule", ["Полный день", "Полный  день"])
    await storage.record_attr_values("experience", ["Без опыта"])
    assert [(v.value_norm, v.seen_count) for v in await storage.get_attr_values("schedule")] == [
        ("полный день", 2)
    ]
    assert [v.label for v in await storage.get_attr_values("experience")] == ["Без опыта"]
    assert await storage.get_attr_values("city") == []


async def test_attr_values_brackets_and_long_labels(storage: Storage) -> None:
    await storage.record_attr_values("city", ["Бохтар (Курган-Тюбе)", "Курган-Тюбе", "я" * 200])
    values = {v.value_norm: v for v in await storage.get_attr_values("city")}
    assert values["бохтар"].seen_count == 2
    assert all(len(v.label) <= 60 for v in values.values())
