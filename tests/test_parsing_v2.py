"""Парсинг v2 (v1.1): нормализация, зарплата, город в карточке, страница объявления."""

from pathlib import Path

import pytest

from jobbot.matcher import matches, normalize
from jobbot.models import Ad, AdDetails, Salary
from jobbot.normalize import CITY_SYNONYMS, normalize_city, normalize_text
from jobbot.parser import parse_detail, parse_listing, parse_salary

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------- нормализация ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Кӯлоб", "кулоб"),
        ("Бохтар (Курган-Тюбе)", "бохтар"),
        ("Полный день", "полный день"),
        ("  ПОЛНЫЙ   ДЕНЬ ", "полный день"),
        ("Ҳисор", "хисор"),
        ("Қайроққум", "кайроккум"),
        ("Ғафуров", "гафуров"),
        ("Ҷаббор Расулов", "чаббор расулов"),
        ("Ӣстаравшан", "истаравшан"),
        ("Ёвон", "евон"),
        ("Бустон (Чкаловск)", "бустон"),
        ("колл--центр", "колл-центр"),
        ("- Удаленно -", "удаленно"),
        ("От\u00a03 лет", "от 3 лет"),
        ("", ""),
    ],
)
def test_normalize_text(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Куляб", "кулоб"),  # так пишет сайт
        ("Кӯлоб", "кулоб"),
        ("Курган-Тюбе", "бохтар"),
        ("Бохтар (Курган-Тюбе)", "бохтар"),
        ("Ленинабад", "худжанд"),
        ("Худжанд", "худжанд"),
        ("Кушониён (Бохтар)", "кушониен"),  # другое место, не путается с Бохтаром
    ],
)
def test_normalize_city_with_synonyms(raw: str, expected: str) -> None:
    assert normalize_city(raw) == expected


def test_city_synonyms_are_normalized_keys() -> None:
    for key, value in CITY_SYNONYMS.items():
        assert normalize_text(key) == key and normalize_text(value) == value


def test_keywords_use_same_normalization_but_keep_brackets() -> None:
    assert normalize("Курьер (Python)") == "курьер (python)"
    assert normalize("Ҳисобчӣ") == "хисобчи"
    assert matches("Курьер (Python)", mode="keywords", include=["python"], exclude=[])
    assert matches("Ҳисобчӣ-стажёр", mode="keywords", include=["стажер"], exclude=[])


# ---------- зарплата (раздел 5.2 ТЗ) ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("900 c.", Salary(900, 900)),
        ("2 000 c.", Salary(2000, 2000)),  # латинская c
        ("2 000 с.", Salary(2000, 2000)),  # кириллическая с
        ("2\u00a0000 c.", Salary(2000, 2000)),  # неразрывный пробел
        ("2\u202f000\u00a0c.", Salary(2000, 2000)),  # узкий неразрывный пробел
        ("от 3 000 c.", Salary(3000, None)),
        ("до 5 000 c.", Salary(None, 5000)),
        ("1 500 - 3 000 c.", Salary(1500, 3000)),
        ("1 500–3 000", Salary(1500, 3000)),  # длинное тире, без валюты
        ("1 500 - 15 000 c.", Salary(1500, 15000)),
        ("от 1 000 до 2 000 c.", Salary(1000, 2000)),
        ("3 000 - 2 000 c.", Salary(2000, 3000)),  # перепутанные края
        ("12 500 сомони", Salary(12500, 12500)),
        ("Договорная", Salary(negotiable=True)),
        ("Договорная цена", Salary(negotiable=True)),
        ("VIP900 c.", Salary(900, 900)),  # значок склеен с ценой
        ("VIPДоговорная", Salary(negotiable=True)),
        ("ТОПДоговорная", Salary(negotiable=True)),
        ("TOP 3 000 c.", Salary(3000, 3000)),
        ("Срочно 1 500 c.", Salary(1500, 1500)),
        ("VIP ТОП 2 000 c.", Salary(2000, 2000)),  # несколько значков
        ("  3 000   c. ", Salary(3000, 3000)),
    ],
)
def test_parse_salary(text: str, expected: Salary) -> None:
    assert parse_salary(text) == expected


@pytest.mark.parametrize("text", [None, "", "   "])
def test_parse_salary_empty_is_unknown_without_warning(
    text: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="parser"):
        assert parse_salary(text) == Salary()
    assert caplog.text == ""


@pytest.mark.parametrize("text", ["по результатам собеседования", "$500", "1,5 млн", "от и до"])
def test_parse_salary_unknown_format_warns(text: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="parser"):
        salary = parse_salary(text)
    assert salary == Salary() and not salary.known
    assert "зарплата не распознана" in caplog.text and text in caplog.text


def test_salary_known_flag() -> None:
    assert Salary(negotiable=True).known
    assert Salary(min=1).known and Salary(max=1).known
    assert not Salary().known


def test_all_salaries_on_real_pages_are_recognized(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="parser"):
        for name, key in (("it_page1.html", "it"), ("students_page1.html", "students")):
            for ad in parse_listing(load(name), key):
                assert parse_salary(ad.salary_text).known, ad.salary_text
    assert caplog.text == ""


# ---------- город в карточке ----------


def test_city_norm_from_card() -> None:
    ads = parse_listing(load("it_page1.html"), "it")
    assert {ad.city_norm for ad in ads} >= {"душанбе", "худжанд"}
    assert all(ad.city_norm == normalize_city(ad.city) for ad in ads if ad.city)
    assert Ad(1, "https://somon.tj/adv/1_x/", "t", "it", city="Куляб").city_norm == "кулоб"
    assert Ad(1, "https://somon.tj/adv/1_x/", "t", "it").city_norm is None


# ---------- страница объявления (раздел 5.3 ТЗ) ----------


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        (
            "detail_salary_range.html",
            AdDetails("Полный день", "Любой", "Душанбе", "Колл центр", "ул. Бохтар 37/1"),
        ),
        (
            "detail_no_experience.html",
            AdDetails(
                "Свободный график", "Без опыта", "Душанбе", "Банк", "ЗАО МДО «Инновация Капитал»"
            ),
        ),
        (
            "detail_remote.html",
            AdDetails("Удаленно", "Любой", "Худжанд", "Сайт объявлений", "улица Пушкина 10"),
        ),
    ],
)
def test_parse_detail_on_real_pages(fixture: str, expected: AdDetails) -> None:
    assert parse_detail(load(fixture)) == expected


def test_parse_detail_labels_are_normalized_and_unknown_ignored() -> None:
    html = """
    <dl>
      <dt>  ГРАФИК : </dt><dd> <a href="/x---1/">Посменно</a> </dd>
      <dt>Стаж:</dt><dd>От 3 лет</dd>
      <dt>Зарплата:</dt><dd>не поле фильтра</dd>
      <dt>Сфера деятельности  компании:</dt><dd>IT</dd>
    </dl>
    """
    assert parse_detail(html) == AdDetails(schedule="Посменно", experience="От 3 лет", sphere="IT")


def test_parse_detail_missing_fields_are_none() -> None:
    assert parse_detail("<html><body><p>Нет характеристик</p></body></html>") == AdDetails()
    assert parse_detail("") == AdDetails()


def test_parse_detail_long_values_are_truncated() -> None:
    details = parse_detail(f"<dl><dt>График:</dt><dd>{'я' * 500}</dd></dl>")
    assert details.schedule is not None and len(details.schedule) == 100


def test_parse_detail_city_from_location_block() -> None:
    html = (
        '<div data-component="AdvertLocationApp"><div><span>Город<!-- -->:</span>'
        "<span>Кӯлоб</span></div></div>"
    )
    assert parse_detail(html).city == "Кӯлоб"
