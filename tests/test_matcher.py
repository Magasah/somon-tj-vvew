"""Тесты фильтров: нормализация, include/exclude, режимы."""

import pytest

from jobbot.config import DEFAULT_EXCLUDE, DEFAULT_INCLUDE
from jobbot.matcher import contains_word, matches, normalize, validate_keyword


def test_normalize() -> None:
    assert normalize("  Ёлка   В ЛЕСУ \n") == "елка в лесу"
    assert normalize("") == ""


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Python разработчик", True),
        ("PYTHON Developer", True),  # регистр
        ("Ведущий Разработчика backend", True),
        ("Стажёр в отдел продаж", True),  # ё → е: в include «стажер»
        ("Стажер-программист", True),
        ("Стажировка без опыта", True),
        ("Работа БЕЗ  ОПЫТА", True),  # фраза, лишние пробелы
        ("1С программист", True),
        ("Продавец-консультант", False),
        ("Водитель", False),
    ],
)
def test_keywords_mode_default_include(title: str, expected: bool) -> None:
    assert (
        matches(title, mode="keywords", include=DEFAULT_INCLUDE, exclude=DEFAULT_EXCLUDE)
        is expected
    )


def test_all_mode_passes_everything_except_exclude() -> None:
    kwargs = {"mode": "all", "include": DEFAULT_INCLUDE, "exclude": DEFAULT_EXCLUDE}
    assert matches("Водитель", **kwargs)
    assert not matches("Оператор Колл-центра", **kwargs)
    assert not matches("Оператор колл центр", **kwargs)
    assert not matches("Call Center agent", **kwargs)


def test_exclude_beats_include() -> None:
    assert not matches(
        "Python разработчик в колл-центр",
        mode="keywords",
        include=["python"],
        exclude=["колл-центр"],
    )


def test_exclude_with_yo_and_case() -> None:
    assert not matches("Ёжик ПРОДАВЕЦ", mode="all", include=[], exclude=["ежик"])


def test_keywords_mode_with_empty_include_passes_nothing() -> None:
    assert not matches("Python", mode="keywords", include=[], exclude=[])


def test_short_words_match_only_as_whole_words() -> None:
    assert contains_word(normalize("IT специалист"), "it")
    assert contains_word(normalize("Работа в IT."), "it")
    assert contains_word(normalize("web-разработчик"), "web")
    assert not contains_word(normalize("Digital маркетолог"), "it")
    assert not contains_word(normalize("Credit manager"), "it")
    assert not contains_word(normalize("Website admin"), "web")


def test_long_words_match_with_endings_but_not_in_the_middle() -> None:
    assert contains_word(normalize("Нужны программисты"), "программист")
    assert contains_word(normalize("Python-разработчика"), "разработчик")
    assert not contains_word(normalize("Суперпрограммист"), "программист")
    assert not contains_word(normalize("Cobweb"), "web")


def test_words_with_special_characters() -> None:
    assert contains_word(normalize("Senior C# developer"), "c#")
    assert contains_word(normalize("Разработчик C++"), "c++")
    assert contains_word(normalize("ASP.NET разработчик"), ".net")
    assert not contains_word(normalize("Python"), "c#")


def test_empty_word_never_matches() -> None:
    assert not contains_word("python", "")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Python ", "python"),
        ("Стажёр", "стажер"),
        ("c#", "c#"),
        ("C++", "c++"),
        (".NET", ".net"),
        ("колл-центр", "колл-центр"),
        ("без   опыта", "без опыта"),
        ("1с", "1с"),
        ("a" * 40, "a" * 40),
    ],
)
def test_validate_keyword_ok(raw: str, expected: str) -> None:
    assert validate_keyword(raw) == expected


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("", "короткое"),
        ("a", "короткое"),
        ("   ", "короткое"),
        ("a" * 41, "длинное"),
        ("py<script>", "Недопустимые"),
        ("drop;table", "Недопустимые"),
        ("слово!", "Недопустимые"),
        ("a/b", "Недопустимые"),
    ],
)
def test_validate_keyword_errors(raw: str, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        validate_keyword(raw)
