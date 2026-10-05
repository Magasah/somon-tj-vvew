"""Модели данных."""

from __future__ import annotations

from dataclasses import dataclass


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
