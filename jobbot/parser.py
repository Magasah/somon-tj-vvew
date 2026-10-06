"""Разбор HTML страницы раздела somon.tj → список объявлений.

Разметка изучена по реальным страницам (tests/fixtures/*.html), селекторы описаны
в PROGRESS.md → «Заметки по парсеру». Всё, что пришло с сайта, — недоверенные данные:
URL проверяется, длина полей ограничивается.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from itertools import pairwise
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobbot.config import BASE_URL
from jobbot.models import Ad, AdDetails, Salary
from jobbot.normalize import normalize_text

log = logging.getLogger("parser")

AD_URL_PREFIX = f"{BASE_URL}/adv/"
AD_HREF_RE = re.compile(r"/adv/(\d+)_")

MAX_TITLE_LEN = 300
MAX_FIELD_LEN = 100  # зарплата, город, дата

CARD_SELECTOR = '[data-component^="JobCard"]'  # JobCardDesktop; мобильная версия — JobCardMobile
LINK_SELECTOR = 'a[itemprop="url"]'


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Результат разбора страницы. simplified=True — сработал запасной парсер."""

    ads: list[Ad]
    simplified: bool = False


def _clean(text: str | None, limit: int) -> str | None:
    """Схлопнуть пробелы (включая неразрывные), обрезать длину; пустое → None."""
    if not text:
        return None
    collapsed = " ".join(text.split())
    return collapsed[:limit] or None


def _ad_url_and_id(href: str | None) -> tuple[int, str] | None:
    """Из href получить (ad_id, канонический абсолютный URL) либо None, если ссылка не наша."""
    if not href:
        return None
    absolute = urljoin(BASE_URL, href.strip())
    parts = urlsplit(absolute)
    clean_url = f"{parts.scheme}://{parts.netloc}{parts.path}"  # без query и fragment
    if not clean_url.startswith(AD_URL_PREFIX):
        return None
    match = AD_HREF_RE.match(parts.path)
    if not match:
        return None
    return int(match.group(1)), clean_url


def _child_elements(node: LexborNode) -> list[LexborNode]:
    return list(node.iter(include_text=False))


def _salary(card: LexborNode) -> str | None:
    """Зарплата: последний <span> без data-slot во второй строке первого блока карточки.

    Первая строка блока — заголовок и кнопка «в избранное», вторая — метка VIP/ТОП
    (span[data-slot=tag], может не быть) и текст зарплаты.
    """
    blocks = [child for child in _child_elements(card) if child.tag == "div"]
    if not blocks:
        return None
    rows = _child_elements(blocks[0])
    if len(rows) < 2:
        return None
    spans = [s for s in rows[1].css("span") if s.attributes.get("data-slot") is None]
    return _clean(spans[-1].text(), MAX_FIELD_LEN) if spans else None


def _date_and_city(card: LexborNode) -> tuple[str | None, str | None]:
    """Дата и город: последний <p> карточки вида «Сегодня · Душанбе» (между ними иконка)."""
    paragraphs = card.css("p")
    if not paragraphs:
        return None, None
    parts = [
        text
        for chunk in paragraphs[-1].text(separator="|").split("|")
        if (text := _clean(chunk, MAX_FIELD_LEN))
    ]
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[-1]


def _title(card: LexborNode, link: LexborNode) -> str | None:
    meta = card.css_first('meta[itemprop="title"]')
    title = _clean(meta.attributes.get("content") if meta else None, MAX_TITLE_LEN)
    return title or _clean(link.text(), MAX_TITLE_LEN)


def parse_listing(html: str, category_key: str) -> list[Ad]:
    """Основной парсер: карточки вакансий `JobCard*`. Пустой список, если карточек нет."""
    tree = LexborHTMLParser(html)
    ads: list[Ad] = []
    seen: set[int] = set()
    for card in tree.css(CARD_SELECTOR):
        link = card.css_first(LINK_SELECTOR)
        if link is None:
            continue
        found = _ad_url_and_id(link.attributes.get("href"))
        title = _title(card, link)
        if found is None or title is None:
            log.debug("карточка пропущена: нет ссылки или заголовка")
            continue
        ad_id, url = found
        if ad_id in seen:
            continue
        seen.add(ad_id)
        date_label, city = _date_and_city(card)
        ads.append(
            Ad(
                ad_id=ad_id,
                url=url,
                title=title,
                category_key=category_key,
                salary_text=_salary(card),
                city=city,
                date_label=date_label,
            )
        )
    return ads


def parse_listing_fallback(html: str, category_key: str) -> list[Ad]:
    """Запасной парсер: ссылки `<a href="/adv/<id>_...">` в разметке страницы.

    Берутся только настоящие <a> из DOM, а не весь текст: в JSON внутри страницы лежат
    объявления из чужих разделов (галереи), их брать нельзя.
    """
    tree = LexborHTMLParser(html)
    ads: list[Ad] = []
    seen: set[int] = set()
    for link in tree.css("a[href]"):
        found = _ad_url_and_id(link.attributes.get("href"))
        if found is None:
            continue
        ad_id, url = found
        title = _clean(link.text(), MAX_TITLE_LEN)
        if ad_id in seen or title is None:
            continue
        seen.add(ad_id)
        ads.append(Ad(ad_id=ad_id, url=url, title=title, category_key=category_key))
    return ads


def parse_page(html: str, category_key: str) -> ParseResult:
    """Основной парсер; если он ничего не нашёл — запасной (результат помечается simplified)."""
    ads = parse_listing(html, category_key)
    if ads:
        return ParseResult(ads)
    fallback = parse_listing_fallback(html, category_key)
    if fallback:
        log.warning("%s: основной парсер пуст, сработал запасной (%d)", category_key, len(fallback))
    return ParseResult(fallback, simplified=bool(fallback))


# ---------- v1.1: зарплата ----------

# Значки продвижения, которые на сайте бывают склеены с ценой («VIP900 c.», «ТОПДоговорная»).
SALARY_BADGES: tuple[str, ...] = ("vip", "топ", "top", "срочно", "премиум", "premium")
_BADGES_RE = re.compile(r"^(?:(?:" + "|".join(SALARY_BADGES) + r")[\s:·,-]*)+", re.IGNORECASE)
_NUMBER = r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+"  # 2 000, 2\u00a0000, 2000
_CURRENCY = r"(?:\s*(?:c|с|сом|сомони|tjs|смн)\.?)?"
_RANGE_RE = re.compile(rf"^({_NUMBER})\s*[-–—]\s*({_NUMBER}){_CURRENCY}$")
_FROM_RE = re.compile(rf"^от\s*({_NUMBER})(?:\s*до\s*({_NUMBER}))?{_CURRENCY}$")
_TO_RE = re.compile(rf"^до\s*({_NUMBER}){_CURRENCY}$")
_SINGLE_RE = re.compile(rf"^({_NUMBER}){_CURRENCY}$")


def _to_int(number: str) -> int:
    return int(re.sub(r"\D", "", number))


def parse_salary(text: str | None) -> Salary:
    """Текст зарплаты из карточки → Salary.

    `900 c.` → 900–900; `1 500 - 3 000 c.` → 1500–3000; `от 3 000` → 3000–∞; `до 5 000` → ∞–5000;
    `Договорная` → negotiable. Значки VIP/ТОП в начале отбрасываются. Нераспознанный текст →
    пустая Salary и предупреждение в лог (без падения).
    """
    if not text or not text.strip():
        return Salary()
    value = " ".join(text.split())  # неразрывные пробелы тоже схлопываются
    value = _BADGES_RE.sub("", value).strip().lower()
    if value.startswith("договорн"):
        return Salary(negotiable=True)
    if match := _RANGE_RE.match(value):
        low, high = sorted((_to_int(match.group(1)), _to_int(match.group(2))))
        return Salary(min=low, max=high)
    if match := _FROM_RE.match(value):
        low = _to_int(match.group(1))
        high = _to_int(match.group(2)) if match.group(2) else None
        if high is not None and high < low:
            low, high = high, low
        return Salary(min=low, max=high)
    if match := _TO_RE.match(value):
        return Salary(max=_to_int(match.group(1)))
    if match := _SINGLE_RE.match(value):
        amount = _to_int(match.group(1))
        return Salary(min=amount, max=amount)
    log.warning("зарплата не распознана: %r", text[:MAX_FIELD_LEN])
    return Salary()


# ---------- v1.1: страница объявления ----------

DETAIL_FEATURES = '[data-component="AdvertFeaturesApp"]'
DETAIL_LOCATION = '[data-component="AdvertLocationApp"]'

# Метка на сайте (после нормализации, без двоеточия) → поле AdDetails.
DETAIL_LABELS: dict[str, str] = {
    "график": "schedule",
    "стаж": "experience",
    "город": "city",
    "сфера деятельности компании": "sphere",
    "название, адрес компании": "company",
}


def _label_key(label: str) -> str | None:
    return DETAIL_LABELS.get(normalize_text(label.rstrip(":  ")))


def parse_detail(html: str) -> AdDetails:
    """Страница объявления → AdDetails (график, стаж, город, сфера, компания).

    Пары «метка: значение» берутся из `dt`/`dd` блока характеристик (а если его нет — из
    любых `dt`/`dd` страницы) и из блока города. Незнакомые метки игнорируются, отсутствующие
    поля — None. Значения — как на сайте (без нормализации), длина ограничена.
    """
    tree = LexborHTMLParser(html)
    fields: dict[str, str] = {}

    container = tree.css_first(DETAIL_FEATURES) or tree.body
    rows = container.css("dt") if container is not None else []
    for dt in rows:
        dd = dt.next
        while dd is not None and dd.tag != "dd":
            dd = dd.next
        key = _label_key(dt.text())
        value = _clean(dd.text(separator=" ") if dd is not None else None, MAX_FIELD_LEN)
        if key and value and key not in fields:
            fields[key] = value

    location = tree.css_first(DETAIL_LOCATION)
    if location is not None:
        spans = [span.text() for span in location.css("span")]
        for label, value in pairwise(spans):
            if _label_key(label) == "city" and "city" not in fields:
                city = _clean(value, MAX_FIELD_LEN)
                if city:
                    fields["city"] = city
                break

    return AdDetails(**fields)
