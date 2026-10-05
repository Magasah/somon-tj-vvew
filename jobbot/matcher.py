"""Фильтры вакансий: нормализация текста, слова include/exclude, режимы разделов."""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from typing import Literal

Mode = Literal["all", "keywords"]

# Слова короче этого порога ищем только как отдельное слово («it», «web», «1с»),
# иначе «it» находилось бы внутри «digital», «credit» и т.п.
SHORT_WORD_LEN = 3


def normalize(text: str) -> str:
    """Нижний регистр, `ё` → `е`, лишние пробелы (в том числе неразрывные) схлопнуты."""
    return " ".join(text.lower().replace("ё", "е").split())


@lru_cache(maxsize=512)
def _word_pattern(word: str) -> re.Pattern[str]:
    """Регулярное выражение для уже нормализованного слова или фразы.

    - Слово не должно начинаться в середине другого слова: «web» не найдётся в «cobweb».
    - Длинные слова допускают окончания (разработчик → разработчика, стажер → стажеров).
    - Короткие слова (≤ 3 символов) должны совпасть целиком.
    - Границы проверяются только у букв и цифр: для «c#», «.net», «c++» это не мешает.
    """
    start = r"(?<!\w)" if word[0].isalnum() else ""
    whole = len(word) <= SHORT_WORD_LEN and word[-1].isalnum()
    end = r"(?!\w)" if whole else ""
    return re.compile(start + re.escape(word) + end)


def contains_word(normalized_text: str, word: str) -> bool:
    """Есть ли слово (фраза) в нормализованном тексте. `word` тоже должно быть нормализовано."""
    return bool(word) and _word_pattern(word).search(normalized_text) is not None


def contains_any(normalized_text: str, words: Iterable[str]) -> bool:
    return any(contains_word(normalized_text, word) for word in words)


def matches(
    text: str,
    *,
    mode: Mode,
    include: Iterable[str],
    exclude: Iterable[str],
) -> bool:
    """Проходит ли объявление фильтр (раздел 3.3 ТЗ).

    - Слово из exclude отклоняет объявление в любом режиме.
    - Режим `all`: проходят все, кроме exclude.
    - Режим `keywords`: нужно хотя бы одно слово из include
      (пустой include → не проходит ничего).
    Слова в include/exclude должны быть нормализованы (storage хранит их уже такими).
    """
    normalized = normalize(text)
    if contains_any(normalized, exclude):
        return False
    if mode == "all":
        return True
    return contains_any(normalized, include)


MIN_WORD_LEN = 2
MAX_WORD_LEN = 40
MAX_WORDS_PER_LIST = 100
_EXTRA_ALLOWED = set(" -+#.")


def validate_keyword(raw: str) -> str:
    """Проверить слово из команды /include, /exclude, /remove и вернуть нормализованное.

    Правила (раздел 3.5 ТЗ): от 2 до 40 символов; только буквы, цифры, пробел, `-`, `+`, `#`, `.`.
    При нарушении бросает ValueError с понятным текстом для владельца.
    """
    word = normalize(raw)
    if len(word) < MIN_WORD_LEN:
        raise ValueError(f"Слово слишком короткое: нужно минимум {MIN_WORD_LEN} символа.")
    if len(word) > MAX_WORD_LEN:
        raise ValueError(f"Слово слишком длинное: максимум {MAX_WORD_LEN} символов.")
    bad = sorted({ch for ch in word if not (ch.isalnum() or ch in _EXTRA_ALLOWED)})
    if bad:
        shown = " ".join(bad)
        raise ValueError(
            f"Недопустимые символы: {shown}. Разрешены буквы, цифры, пробел и знаки - + # ."
        )
    return word
