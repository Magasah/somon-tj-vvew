"""Тесты парсера на реальных страницах somon.tj (tests/fixtures)."""

from pathlib import Path

import pytest

from jobbot.parser import parse_listing, parse_listing_fallback, parse_page

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("fixture", "key"), [("it_page1.html", "it"), ("students_page1.html", "students")]
)
def test_main_parser_on_real_pages(fixture: str, key: str) -> None:
    ads = parse_listing(load(fixture), key)
    assert len(ads) >= 10
    assert len({a.ad_id for a in ads}) == len(ads)  # без дублей
    for ad in ads:
        assert ad.ad_id > 0
        assert ad.title
        assert len(ad.title) <= 300
        assert ad.url.startswith("https://somon.tj/adv/")
        assert f"/adv/{ad.ad_id}_" in ad.url
        assert ad.category_key == key
    # поля зарплаты, города и даты на реальных страницах есть у всех карточек
    assert all(a.salary_text and a.city and a.date_label for a in ads)


def test_first_card_fields() -> None:
    ad = parse_listing(load("it_page1.html"), "it")[0]
    assert ad.ad_id == 11530914
    assert ad.title == "Технический специалист по автоматизации"
    assert ad.salary_text == "5 000 c."
    assert ad.city == "Душанбе"
    assert ad.date_label == "Сегодня"


def test_main_parser_ignores_foreign_ads_from_json() -> None:
    # В JSON внутри страницы лежат объявления из других разделов — их быть не должно.
    html = load("it_page1.html")
    assert "12704064" in html
    assert 12704064 not in {a.ad_id for a in parse_listing(html, "it")}


def test_fallback_parser_on_html_without_cards() -> None:
    html = load("it_page1.html").replace('data-component="JobCardDesktop"', "")
    assert parse_listing(html, "it") == []
    ads = parse_listing_fallback(html, "it")
    assert len(ads) >= 10
    assert 12704064 not in {a.ad_id for a in ads}
    assert all(a.title and a.url.startswith("https://somon.tj/adv/") for a in ads)

    result = parse_page(html, "it")
    assert result.simplified
    assert len(result.ads) == len(ads)


def test_parse_page_uses_main_parser_when_possible() -> None:
    result = parse_page(load("students_page1.html"), "students")
    assert not result.simplified
    assert len(result.ads) >= 10


def test_empty_html_gives_no_ads() -> None:
    result = parse_page("<html><body>ничего</body></html>", "it")
    assert result.ads == []
    assert not result.simplified


def test_foreign_and_broken_links_are_rejected() -> None:
    html = """
    <a href="https://evil.example/adv/1_x/">чужой сайт</a>
    <a href="/adv/abc_x/">без числа</a>
    <a href="/adv/42_ok/?utm=1#top">  Нормальная   вакансия </a>
    <a href="/adv/42_ok/">дубль</a>
    """
    ads = parse_listing_fallback(html, "it")
    assert [(a.ad_id, a.url, a.title) for a in ads] == [
        (42, "https://somon.tj/adv/42_ok/", "Нормальная вакансия")
    ]


def test_long_title_is_truncated() -> None:
    html = f'<a href="/adv/7_x/">{"я" * 1000}</a>'
    assert len(parse_listing_fallback(html, "it")[0].title) == 300


# ---------- v1.1, шаг 9.1: селекторы страницы объявления (реальные фикстуры) ----------

DETAIL_EXPECTED = {
    # фикстура: (цена, город, график, стаж)
    "detail_salary_range.html": ("3 000 - 4 000 c.", "Душанбе", "Полный день", "Любой"),
    "detail_no_experience.html": ("Договорная", "Душанбе", "Свободный график", "Без опыта"),
    "detail_remote.html": ("900 c.", "Худжанд", "Удаленно", "Любой"),
}


@pytest.mark.parametrize(("fixture", "expected"), DETAIL_EXPECTED.items())
def test_detail_page_selectors(fixture: str, expected: tuple[str, str, str, str]) -> None:
    from selectolax.lexbor import LexborHTMLParser

    tree = LexborHTMLParser(load(fixture))
    pairs = {
        row.css_first("dt").text(strip=True): " ".join(row.css_first("dd").text().split())
        for row in tree.css('[data-component="AdvertFeaturesApp"] dl > div')
    }
    price = tree.css_first('[data-component="SidebarPrice"]').text(strip=True)
    location = [s.text(strip=True) for s in tree.css('[data-component="AdvertLocationApp"] span')]

    assert (price, location[1], pairs["График:"], pairs["Стаж:"]) == expected
    assert location[0] == "Город:"
    assert {"Сфера деятельности компании:", "Название, адрес компании:"} <= pairs.keys()
