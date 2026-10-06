"""Каталог категорий, настройки v1.1 (VISIBLE_CATEGORIES, MAX_DETAILS_PER_CYCLE), FilterProfile."""

import pytest
from pydantic import ValidationError

from jobbot.catalog import CATALOG, CATALOG_BY_KEY, parse_category_keys
from jobbot.config import DEFAULT_CATEGORIES, Settings, load_settings
from jobbot.fetcher import check_url_allowed
from jobbot.filters import FilterProfile, SalaryFilter, dump_profile, load_profile

# ---------- каталог ----------


def test_catalog_has_26_unique_categories() -> None:
    assert len(CATALOG) == 26
    assert len(CATALOG_BY_KEY) == 26
    assert len({c.slug for c in CATALOG}) == 26


@pytest.mark.parametrize(
    ("key", "slug"),
    [
        ("admin", "cekretariat--aho"),
        ("it", "it--telekom--kompyuteryi"),
        ("students", "nachalo-kareryi--studentyi"),
        ("banks", "Banki-strahovanie-lizing"),  # регистр как на сайте
        ("restaurants", "Restoratori_povara_ofisianti"),  # подчёркивания как на сайте
        ("construction", "stroitelstvo"),
        ("abroad", "rabota-za-rubejom"),
    ],
)
def test_catalog_slugs_exactly_as_on_site(key: str, slug: str) -> None:
    assert CATALOG_BY_KEY[key].slug == slug
    assert CATALOG_BY_KEY[key].url == f"https://somon.tj/vakansii/{slug}/"


def test_all_catalog_urls_are_allowed_by_rules() -> None:
    for category in CATALOG:
        check_url_allowed(category.url)  # нет `---`, запрещённых путей и параметров


def test_default_categories_unchanged_for_v1_compatibility() -> None:
    assert [(c.key, c.url) for c in DEFAULT_CATEGORIES] == [
        ("it", "https://somon.tj/vakansii/it--telekom--kompyuteryi/"),
        ("students", "https://somon.tj/vakansii/nachalo-kareryi--studentyi/"),
    ]


def test_parse_category_keys() -> None:
    assert parse_category_keys("it,students") == ("it", "students")
    assert parse_category_keys(" IT , sales,it ") == ("it", "sales")
    with pytest.raises(ValueError, match="неизвестный раздел"):
        parse_category_keys("it,nope")
    with pytest.raises(ValueError, match="хотя бы один"):
        parse_category_keys(" , ")


# ---------- настройки ----------


def test_settings_defaults_for_v11() -> None:
    settings = Settings(_env_file=None)
    assert settings.visible_keys == ("it", "students")
    assert settings.max_details_per_cycle == 10
    assert [c.key for c in settings.categories] == ["it", "students"]


def test_visible_categories_opens_new_section_without_code() -> None:
    settings = Settings(_env_file=None, visible_categories="it,students,sales")
    assert [c.key for c in settings.categories] == ["it", "students", "sales"]
    assert (
        settings.categories[2].url == "https://somon.tj/vakansii/prodazhi--roznichnaya-torgovlya/"
    )


@pytest.mark.parametrize(
    "env",
    [
        {"VISIBLE_CATEGORIES": "it,nope"},
        {"MAX_DETAILS_PER_CYCLE": "0"},
        {"MAX_DETAILS_PER_CYCLE": "100"},
    ],
)
def test_bad_v11_settings_exit_with_code_1(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("OWNER_CHAT_ID", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as exc:
        load_settings()
    assert exc.value.code == 1


# ---------- модель профиля ----------


def test_profile_defaults() -> None:
    profile = FilterProfile()
    assert profile.version == 1
    assert profile.categories == ["it", "students"]
    assert profile.cities == [] and profile.schedules == [] and profile.experience == []
    assert profile.salary == SalaryFilter(min=None, max=None, include_negotiable=True)
    assert profile.include_unknown_attrs is True
    assert not profile.needs_details


def test_profile_json_roundtrip() -> None:
    profile = FilterProfile(
        categories=["it"],
        cities=["душанбе"],
        salary=SalaryFilter(min=2000, max=5000, include_negotiable=False),
        schedules=["полный день"],
        experience=["без опыта"],
        include_unknown_attrs=False,
    )
    assert load_profile(dump_profile(profile)) == profile
    assert profile.needs_details


def test_profile_cleans_duplicates_and_spaces() -> None:
    profile = FilterProfile(categories=["it", "it"], cities=[" душанбе ", "душанбе", ""])
    assert profile.categories == ["it"]
    assert profile.cities == ["душанбе"]


@pytest.mark.parametrize(
    "data",
    [
        {"categories": []},
        {"categories": ["nope"]},
        {"cities": [f"г{i}" for i in range(31)]},
        {"schedules": [f"г{i}" for i in range(16)]},
        {"experience": [f"с{i}" for i in range(16)]},
        {"cities": ["я" * 61]},
        {"salary": {"min": -1}},
        {"salary": {"max": 1_000_001}},
        {"salary": {"min": 5000, "max": 2000}},
    ],
)
def test_profile_validation_errors(data: dict) -> None:
    with pytest.raises(ValidationError):
        FilterProfile.model_validate(data)


def test_profile_limits_are_inclusive() -> None:
    FilterProfile(
        cities=[f"г{i}" for i in range(30)],
        schedules=[f"г{i}" for i in range(15)],
        experience=[f"с{i}" for i in range(15)],
        salary=SalaryFilter(min=0, max=1_000_000),
    )
    FilterProfile(salary=SalaryFilter(min=3000, max=3000))


def test_load_profile_ignores_unknown_fields_and_survives_garbage() -> None:
    assert load_profile('{"categories": ["it"], "future_field": 1}').categories == ["it"]
    assert load_profile(None) == FilterProfile()
    assert load_profile("") == FilterProfile()
    assert load_profile("[1, 2, 3]") == FilterProfile()
