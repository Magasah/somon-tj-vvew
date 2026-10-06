"""Каталог разделов вакансий somon.tj (все 26). Слаги — ровно как на сайте, включая регистр.

Какие разделы видны в боте, задаёт `VISIBLE_CATEGORIES` в .env (например, `it,students`).
Открыть новый раздел = дописать его ключ в эту строку, код менять не нужно.
"""

from __future__ import annotations

from dataclasses import dataclass

SITE_URL = "https://somon.tj"


@dataclass(frozen=True, slots=True)
class CatalogCategory:
    key: str
    title: str
    slug: str

    @property
    def url(self) -> str:
        return f"{SITE_URL}/vakansii/{self.slug}/"


CATALOG: tuple[CatalogCategory, ...] = (
    CatalogCategory("admin", "Административный персонал", "cekretariat--aho"),
    CatalogCategory("finance", "Бухгалтерия, финансы, юристы", "buhgalteriya--yuristyi--finansyi"),
    CatalogCategory("marketing", "Маркетинг, реклама, дизайн", "marketing--reklama--dizajn"),
    CatalogCategory("security", "Охрана, безопасность", "ohrana--bezopasnost"),
    CatalogCategory("workers", "Рабочий персонал, разнорабочие", "rabochi-personal-raznorabochii"),
    CatalogCategory("transport", "Транспорт, логистика, склад", "transport--logistika"),
    CatalogCategory("agencies", "Кадровые агентства", "kadrovyie-agentstva"),
    CatalogCategory("hr", "HR, кадры", "hr--kadryi"),
    CatalogCategory("gov", "Государственные службы", "gosudarstvennie-slujbi"),
    CatalogCategory("medicine", "Медицина, фармация", "meditsina--farmatsiya"),
    CatalogCategory("sales", "Продажи, розничная торговля", "prodazhi--roznichnaya-torgovlya"),
    CatalogCategory("managers", "Руководители", "rukovoditeli"),
    CatalogCategory("tourism", "Туризм, гостиницы, развлечения", "turizm--gostinici--razvlecheniya"),
    CatalogCategory("other", "Другие сферы занятий", "drugie-sferyi-zanyatij"),
    CatalogCategory("it", "IT, телеком, компьютеры", "it--telekom--kompyuteryi"),
    CatalogCategory("domestic", "Домашний персонал, обслуживание", "domashnij-personal--obsluzhivanie"),
    CatalogCategory("students", "Начало карьеры, студенты", "nachalo-kareryi--studentyi"),
    CatalogCategory("production", "Производство, энергетика", "proizvodstvo--energetika"),
    CatalogCategory("media", "СМИ, издательство", "smi--izdatelstvo"),
    CatalogCategory("parttime", "Частичная занятость", "chastichnaya-zanyatost"),
    CatalogCategory("banks", "Банки, страхование, лизинг", "Banki-strahovanie-lizing"),
    CatalogCategory("beauty", "Красота, фитнес, спорт", "krasota--fitnes--sport"),
    CatalogCategory("education", "Образование, культура, искусство", "obrazovanie--kultura--iskusstvo"),
    CatalogCategory("restaurants", "Рестораторы, повара, официанты", "Restoratori_povara_ofisianti"),
    CatalogCategory("construction", "Строительство, недвижимость", "stroitelstvo"),
    CatalogCategory("abroad", "Работа за рубежом", "rabota-za-rubejom"),
)  # fmt: skip

CATALOG_BY_KEY: dict[str, CatalogCategory] = {c.key: c for c in CATALOG}

DEFAULT_VISIBLE: tuple[str, ...] = ("it", "students")


def parse_category_keys(raw: str) -> tuple[str, ...]:
    """Строка `it, students` → ('it', 'students'). Неизвестный ключ → ValueError с подсказкой."""
    keys: list[str] = []
    for part in raw.split(","):
        key = part.strip().lower()
        if not key or key in keys:
            continue
        if key not in CATALOG_BY_KEY:
            known = ", ".join(c.key for c in CATALOG)
            raise ValueError(f"неизвестный раздел «{key}». Допустимые: {known}")
        keys.append(key)
    if not keys:
        raise ValueError("нужен хотя бы один раздел, например: it,students")
    return tuple(keys)
