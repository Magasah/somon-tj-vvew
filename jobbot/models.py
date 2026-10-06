"""Модели данных."""

from __future__ import annotations

from dataclasses import dataclass

from jobbot.normalize import normalize_city


@dataclass(frozen=True, slots=True)
class Ad:
    """Объявление из списка раздела (поля — раздел 3.1 ТЗ)."""

    ad_id: int
    url: str
    title: str
    category_key: str
    salary_text: str | None = None
    city: str | None = None
    date_label: str | None = None
    # v1.1: со страницы объявления (заполняются после загрузки деталей, иначе None)
    schedule: str | None = None
    experience: str | None = None
    company: str | None = None
    sphere: str | None = None
    details_status: str | None = None  # none|pending|ok|failed|skipped

    @property
    def details_unchecked(self) -> bool:
        """График/стаж нужно было проверить, но страница объявления не загрузилась."""
        return self.details_status == "failed"

    @property
    def city_norm(self) -> str | None:
        """Город для сравнения с фильтром (`Куляб` → `кулоб`); None, если города нет."""
        return normalize_city(self.city) if self.city else None


@dataclass(frozen=True, slots=True)
class Salary:
    """Зарплата из текста карточки. Все поля None и negotiable=False — не распознана."""

    min: int | None = None
    max: int | None = None
    negotiable: bool = False  # «Договорная»

    @property
    def known(self) -> bool:
        return self.negotiable or self.min is not None or self.max is not None


@dataclass(frozen=True, slots=True)
class AdDetails:
    """Поля со страницы объявления (v1.1). None — на странице поля нет."""

    schedule: str | None = None  # «График», как на сайте
    experience: str | None = None  # «Стаж»
    city: str | None = None
    sphere: str | None = None  # «Сфера деятельности компании»
    company: str | None = None  # «Название, адрес компании»
