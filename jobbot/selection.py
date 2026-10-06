"""Отбор вакансий по профилю фильтров (v1.1, раздел 6 TZ_update_filters.md).

Двухступенчатый, чтобы открывать страницы объявлений как можно реже:
1. по данным карточки (категория, город, зарплата, ключевые слова) — без запросов к сайту;
2. по странице объявления (график, стаж) — только если эти фильтры выбраны.

Одна и та же логика используется и для новых объявлений, и для подсчётов по базе в меню.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from jobbot.filters import FilterProfile, SalaryFilter
from jobbot.matcher import Mode, matches
from jobbot.models import Ad, AdDetails, Salary
from jobbot.normalize import normalize_city, normalize_text
from jobbot.parser import parse_salary


class Decision(StrEnum):
    REJECT = "reject"  # не подходит
    ACCEPT = "accept"  # подходит, отправлять
    NEED_DETAILS = "need_details"  # карточка подходит, решит страница объявления


@dataclass(frozen=True, slots=True)
class CardFacts:
    """Что известно об объявлении из карточки списка."""

    category_key: str
    title: str
    city_norm: str | None
    salary: Salary

    @classmethod
    def from_ad(cls, ad: Ad) -> CardFacts:
        return cls(ad.category_key, ad.title, ad.city_norm, parse_salary(ad.salary_text))


# ---------- ступень 1: карточка ----------


def city_ok(city_norm: str | None, profile: FilterProfile) -> bool:
    if not profile.cities:
        return True  # «Все города»
    if not city_norm:
        return profile.include_unknown_attrs
    return city_norm in {normalize_city(city) for city in profile.cities}


def salary_ok(salary: Salary, wanted: SalaryFilter, include_unknown: bool) -> bool:
    """Зарплата объявления против фильтра (раздел 6, п. 3).

    «Договорная» — по переключателю. Иначе пересечение диапазонов: у объявления пустой край
    равен другому краю, у фильтра пустой край — без ограничения. Не распознана — по
    `include_unknown_attrs`, но только если в фильтре задана граница.
    """
    if salary.negotiable:
        return wanted.include_negotiable
    limited = wanted.min is not None or wanted.max is not None
    if not limited:
        return True
    ad_min = salary.min if salary.min is not None else salary.max
    ad_max = salary.max if salary.max is not None else salary.min
    if ad_min is None or ad_max is None:
        return include_unknown  # зарплата не распознана
    if wanted.min is not None and ad_max < wanted.min:
        return False
    return not (wanted.max is not None and ad_min > wanted.max)


def check_card(
    facts: CardFacts,
    profile: FilterProfile,
    *,
    keyword_mode: Mode,
    include: Iterable[str],
    exclude: Iterable[str],
) -> Decision:
    """Ступень 1. ACCEPT/REJECT или NEED_DETAILS, если выбраны график или стаж."""
    if facts.category_key not in profile.categories:
        return Decision.REJECT
    if not city_ok(facts.city_norm, profile):
        return Decision.REJECT
    if not salary_ok(facts.salary, profile.salary, profile.include_unknown_attrs):
        return Decision.REJECT
    if not matches(facts.title, mode=keyword_mode, include=include, exclude=exclude):
        return Decision.REJECT
    return Decision.NEED_DETAILS if profile.needs_details else Decision.ACCEPT


# ---------- ступень 2: страница объявления ----------


def _value_ok(value: str | None, wanted: list[str], include_unknown: bool) -> bool:
    if not wanted:
        return True  # «Любой»
    if not value:
        return include_unknown
    return normalize_text(value) in {normalize_text(item) for item in wanted}


def check_details(details: AdDetails, profile: FilterProfile) -> bool:
    """Ступень 2: график и стаж по нормализованным значениям."""
    unknown = profile.include_unknown_attrs
    return _value_ok(details.schedule, profile.schedules, unknown) and _value_ok(
        details.experience, profile.experience, unknown
    )


def initial_details_status(decision: Decision) -> str:
    """`details_status` для нового объявления: `pending` — в очередь страниц, иначе `skipped`."""
    return "pending" if decision is Decision.NEED_DETAILS else "skipped"
