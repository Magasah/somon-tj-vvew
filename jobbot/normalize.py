"""Нормализация текста для сравнения: города, график, стаж, ключевые слова (v1.1).

`Кӯлоб` → `кулоб`, `Бохтар (Курган-Тюбе)` → `бохтар`, `Полный день` → `полный день`.
"""

from __future__ import annotations

import re

# Таджикские буквы → русские аналоги (и ё → е), чтобы «Кӯлоб» и «Кулоб» совпадали.
_LETTERS = str.maketrans({"ё": "е", "ӣ": "и", "ӯ": "у", "ҳ": "х", "қ": "к", "ғ": "г", "ҷ": "ч"})
_BRACKETS_RE = re.compile(r"\([^()]*\)")
_SPACES_RE = re.compile(r"\s+")
_HYPHENS_RE = re.compile(r"-{2,}")

# Синонимы городов: старое или русское название → как храним. Дополнять здесь.
CITY_SYNONYMS: dict[str, str] = {
    "куляб": "кулоб",
    "курган-тюбе": "бохтар",
    "ленинабад": "худжанд",
}


def normalize_text(text: str, *, drop_brackets: bool = True) -> str:
    """Нижний регистр → таджикские буквы и ё → убрать «(…)» → схлопнуть пробелы и дефисы.

    drop_brackets=False — для заголовков вакансий при поиске ключевых слов:
    текст в скобках там бывает важен («Курьер (Python)»).
    """
    value = text.lower().translate(_LETTERS)
    if drop_brackets:
        value = _BRACKETS_RE.sub(" ", value)
    value = _HYPHENS_RE.sub("-", value)
    value = _SPACES_RE.sub(" ", value)  # заодно неразрывные пробелы и переводы строк
    return value.strip(" -")


def normalize_city(name: str) -> str:
    """Город для сравнения: нормализация + синонимы (`Куляб` → `кулоб`)."""
    value = normalize_text(name)
    return CITY_SYNONYMS.get(value, value)
