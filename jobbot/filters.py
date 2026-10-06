"""Профиль фильтров владельца (v1.1): категории, города, зарплата, график, стаж.

Хранится в базе одной JSON-строкой (таблица `filter_profile`). Любые данные из базы
проверяются моделью: битый JSON или недопустимые значения → предупреждение в лог и профиль
по умолчанию, бот не падает.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from jobbot.catalog import CATALOG_BY_KEY, DEFAULT_VISIBLE

log = logging.getLogger("filters")

PROFILE_VERSION = 1
MAX_SALARY = 1_000_000
MAX_CITIES = 30
MAX_SCHEDULES = 15
MAX_EXPERIENCE = 15
MAX_VALUE_LEN = 60  # длина одного значения города/графика/стажа


def _clean_values(values: list[str], limit: int, name: str) -> list[str]:
    """Убрать пустые и повторы (порядок сохраняется), проверить лимиты."""
    result: list[str] = []
    for raw in values:
        value = " ".join(str(raw).split())
        if not value or value in result:
            continue
        if len(value) > MAX_VALUE_LEN:
            raise ValueError(f"{name}: значение длиннее {MAX_VALUE_LEN} символов")
        result.append(value)
    if len(result) > limit:
        raise ValueError(f"{name}: не больше {limit} значений")
    return result


class SalaryFilter(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    min: int | None = Field(default=None, ge=0, le=MAX_SALARY)  # «от», сомони
    max: int | None = Field(default=None, ge=0, le=MAX_SALARY)  # «до», сомони
    include_negotiable: bool = True  # показывать «Договорная»

    @model_validator(mode="after")
    def _check_range(self) -> SalaryFilter:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("зарплата: «от» больше, чем «до»")
        return self


class FilterProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int = PROFILE_VERSION
    categories: list[str] = Field(default_factory=lambda: list(DEFAULT_VISIBLE))
    cities: list[str] = Field(default_factory=list)  # нормализованные; [] = все города
    salary: SalaryFilter = Field(default_factory=SalaryFilter)
    schedules: list[str] = Field(default_factory=list)  # нормализованные; [] = любой
    experience: list[str] = Field(default_factory=list)  # нормализованные; [] = любой
    include_unknown_attrs: bool = True  # пропускать, если поле на сайте не указано

    @field_validator("categories")
    @classmethod
    def _check_categories(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for key in values:
            if key not in CATALOG_BY_KEY:
                raise ValueError(f"неизвестная категория: {key}")
            if key not in result:
                result.append(key)
        if not result:
            raise ValueError("нужна хотя бы одна категория")
        return result

    @field_validator("cities")
    @classmethod
    def _check_cities(cls, values: list[str]) -> list[str]:
        return _clean_values(values, MAX_CITIES, "города")

    @field_validator("schedules")
    @classmethod
    def _check_schedules(cls, values: list[str]) -> list[str]:
        return _clean_values(values, MAX_SCHEDULES, "график")

    @field_validator("experience")
    @classmethod
    def _check_experience(cls, values: list[str]) -> list[str]:
        return _clean_values(values, MAX_EXPERIENCE, "стаж")

    @property
    def needs_details(self) -> bool:
        """Нужны ли страницы объявлений: только если выбран график или стаж."""
        return bool(self.schedules or self.experience)


def load_profile(raw: str | None) -> FilterProfile:
    """JSON из базы → профиль. Пусто или ошибка → профиль по умолчанию (с предупреждением)."""
    if not raw:
        return FilterProfile()
    try:
        return FilterProfile.model_validate_json(raw)
    except ValidationError as exc:
        log.warning("профиль фильтров в базе повреждён, используется профиль по умолчанию: %s", exc)
        return FilterProfile()


def dump_profile(profile: FilterProfile) -> str:
    return profile.model_dump_json()


SALARY_INPUT_HELP = "Примеры: 2000, от 2000, до 5000, 2000-5000"


def parse_salary_input(text: str) -> tuple[int | None, int | None]:
    """Ручной ввод зарплаты в меню → (от, до). Неверный ввод → ValueError с подсказкой.

    `2000` и `от 2000` → (2000, None); `до 5000` → (None, 5000); `2000-5000` → (2000, 5000).
    Пробелы внутри чисел допускаются: `2 000`.
    """
    value = " ".join(text.lower().replace("–", "-").replace("—", "-").split())
    value = re.sub(r"(?<=\d) (?=\d{3}\b)", "", value)  # «2 000» → «2000»
    value = re.sub(r"\s*(c|с|сом|сомони)\.?$", "", value)
    patterns = (
        (r"(\d+)", lambda m: (int(m[1]), None)),
        (r"от ?(\d+)", lambda m: (int(m[1]), None)),
        (r"до ?(\d+)", lambda m: (None, int(m[1]))),
        (r"(\d+) ?- ?(\d+)", lambda m: (int(m[1]), int(m[2]))),
        (r"от ?(\d+) ?до ?(\d+)", lambda m: (int(m[1]), int(m[2]))),
    )
    for pattern, build in patterns:
        match = re.fullmatch(pattern, value)
        if match:
            low, high = build(match)
            break
    else:
        raise ValueError(f"Не понял формат. {SALARY_INPUT_HELP}")
    for amount in (low, high):
        if amount is not None and amount > MAX_SALARY:
            raise ValueError(f"Слишком большая сумма: максимум {MAX_SALARY:,}".replace(",", " "))
    if low is not None and high is not None and low > high:
        raise ValueError(f"«От» больше, чем «до». {SALARY_INPUT_HELP}")
    return low, high
