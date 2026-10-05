"""Тесты notifier: форматирование, экранирование, дайджест, отправка, RetryAfter."""

from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from jobbot.models import Ad
from jobbot.notifier import (
    BUTTON_TEXT,
    SIMPLIFIED_MARK,
    Notifier,
    card_keyboard,
    format_card,
    format_digest,
    format_seed_header,
)

TITLES = {"it": "IT, телеком, компьютеры"}
METHOD = SendMessage(chat_id=1, text="x")


def make_ad(ad_id: int = 1, **overrides: Any) -> Ad:
    fields: dict[str, Any] = {
        "ad_id": ad_id,
        "url": f"https://somon.tj/adv/{ad_id}_x/",
        "title": ".NET Backend разработчик",
        "category_key": "it",
        "salary_text": "5 000 c.",
        "city": "Душанбе",
        "date_label": "Сегодня",
    }
    fields.update(overrides)
    return Ad(**fields)


class FakeBot:
    """Подмена aiogram.Bot: запоминает отправленное, умеет падать заданными ошибками."""

    def __init__(self, errors: list[Exception | None] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self._errors = list(errors or [])

    async def send_message(self, **kwargs: Any) -> None:
        error = self._errors.pop(0) if self._errors else None
        if error is not None:
            raise error
        self.sent.append(kwargs)


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_notifier(bot: FakeBot, sleep: FakeSleep, threshold: int = 8) -> Notifier:
    return Notifier(bot, 42, digest_threshold=threshold, sleep=sleep)  # type: ignore[arg-type]


# ---------- форматирование ----------


def test_card_format_matches_spec() -> None:
    text = format_card(make_ad(), TITLES["it"])
    assert text == (
        "💼 <b>.NET Backend разработчик</b>\n"
        "💰 5 000 c. · 📍 Душанбе · 🕒 Сегодня\n"
        "🏷 IT, телеком, компьютеры"
    )


def test_card_skips_missing_fields() -> None:
    text = format_card(make_ad(salary_text=None, city=None, date_label="Вчера"), "Раздел")
    assert text == "💼 <b>.NET Backend разработчик</b>\n🕒 Вчера\n🏷 Раздел"
    bare = format_card(make_ad(salary_text=None, city=None, date_label=None), "Раздел")
    assert bare.count("\n") == 1  # только заголовок и раздел


def test_card_escapes_everything_from_site() -> None:
    ad = make_ad(
        title="<script>alert(1)</script> & <b>жир</b>",
        salary_text="<i>много</i>",
        city='"><img src=x>',
        date_label="<u>сегодня</u>",
    )
    text = format_card(ad, "<b>Раздел</b>")
    for raw in ("<script>", "<img", "<i>", "<u>", "<b>жир", "<b>Раздел"):
        assert raw not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;жир&lt;/b&gt;" in text
    assert text.startswith("💼 <b>") and "</b>\n" in text  # наша разметка осталась


def test_card_simplified_mark() -> None:
    assert SIMPLIFIED_MARK in format_card(make_ad(), "Раздел", simplified=True)
    assert SIMPLIFIED_MARK not in format_card(make_ad(), "Раздел")


def test_keyboard_button() -> None:
    keyboard = card_keyboard(make_ad(7))
    assert keyboard is not None
    button = keyboard.inline_keyboard[0][0]
    assert button.text == BUTTON_TEXT == "Открыть на Somon"
    assert button.url == "https://somon.tj/adv/7_x/"


def test_keyboard_refuses_foreign_url() -> None:
    assert card_keyboard(make_ad(url="https://evil.example/adv/1_x/")) is None
    assert card_keyboard(make_ad(url="javascript:alert(1)")) is None


def test_seed_header_escaped() -> None:
    assert format_seed_header("A<B") == "📌 <b>Последние вакансии в разделе «A&lt;B»</b>"


@pytest.mark.parametrize(
    ("count", "phrase"),
    [
        (9, "9 новых вакансий"),
        (21, "21 новая вакансия"),
        (23, "23 новые вакансии"),
        (12, "12 новых вакансий"),
    ],
)
def test_digest_header_plural(count: int, phrase: str) -> None:
    messages = format_digest([make_ad(i) for i in range(1, count + 1)])
    assert f"Найдено {phrase}" in messages[0]


def test_digest_lines_and_escaping() -> None:
    ads = [make_ad(1, title="A & <b>"), make_ad(2, title="Second")]
    (message,) = format_digest(ads)
    assert message.splitlines() == [
        "🔔 <b>Найдено 2 новые вакансии</b>",
        "• A &amp; &lt;b&gt; — https://somon.tj/adv/1_x/",
        "• Second — https://somon.tj/adv/2_x/",
    ]


def test_digest_splits_under_telegram_limit() -> None:
    ads = [make_ad(i, title="Ю" * 300) for i in range(1, 60)]
    messages = format_digest(ads)
    assert len(messages) > 1
    assert all(len(m) <= 4096 for m in messages)
    # ни одна строка не потеряна и не порезана
    lines = [line for m in messages for line in m.splitlines() if line.startswith("• ")]
    assert len(lines) == 59
    assert all(line.endswith("_x/") for line in lines)


def test_digest_drops_foreign_urls() -> None:
    bad = make_ad(2, url="https://evil.example/x")
    (message,) = format_digest([make_ad(1), bad])
    assert "evil.example" not in message


# ---------- отправка ----------


async def test_send_cards_one_by_one_with_button_and_pause() -> None:
    bot, sleep = FakeBot(), FakeSleep()
    sent = await make_notifier(bot, sleep).send_ads([make_ad(1), make_ad(2)], TITLES)
    assert sent == [1, 2]
    assert len(bot.sent) == 2
    first = bot.sent[0]
    assert first["chat_id"] == 42
    assert first["parse_mode"] == "HTML"
    assert first["reply_markup"].inline_keyboard[0][0].url == "https://somon.tj/adv/1_x/"
    assert "IT, телеком, компьютеры" in first["text"]
    # между сообщениями выдержана пауза ~1 с (перед первым — без паузы)
    assert len(sleep.calls) == 1
    assert 0.5 < sleep.calls[0] <= 1.0


async def test_more_than_threshold_sends_digest() -> None:
    bot, sleep = FakeBot(), FakeSleep()
    ads = [make_ad(i) for i in range(1, 10)]  # 9 > 8
    sent = await make_notifier(bot, sleep).send_ads(ads, TITLES)
    assert sent == list(range(1, 10))
    assert len(bot.sent) == 1
    assert "Найдено 9 новых вакансий" in bot.sent[0]["text"]
    assert bot.sent[0]["reply_markup"] is None


async def test_exactly_threshold_still_cards() -> None:
    bot = FakeBot()
    await make_notifier(bot, FakeSleep()).send_ads([make_ad(i) for i in range(1, 9)], TITLES)
    assert len(bot.sent) == 8


async def test_header_is_part_of_first_message_only() -> None:
    bot = FakeBot()
    await make_notifier(bot, FakeSleep()).send_ads(
        [make_ad(1), make_ad(2)], TITLES, header=format_seed_header("IT")
    )
    assert len(bot.sent) == 2  # отдельного сообщения под заголовок нет
    assert bot.sent[0]["text"].startswith("📌 <b>Последние вакансии")
    assert "💼" in bot.sent[0]["text"]
    assert bot.sent[1]["text"].startswith("💼")


async def test_header_survives_skipped_first_card() -> None:
    bot = FakeBot([TelegramBadRequest(METHOD, "cannot parse"), None])
    await make_notifier(bot, FakeSleep()).send_ads([make_ad(1), make_ad(2)], TITLES, header="HEAD")
    assert bot.sent[0]["text"].startswith("HEAD\n\n💼")


async def test_header_in_digest_goes_to_first_message() -> None:
    bot = FakeBot()
    ads = [make_ad(i) for i in range(1, 10)]
    await make_notifier(bot, FakeSleep()).send_ads(ads, TITLES, header="HEAD")
    assert bot.sent[0]["text"].startswith("HEAD\n\n🔔")


async def test_retry_after_waits_and_retries() -> None:
    bot = FakeBot([TelegramRetryAfter(METHOD, "flood", 7)])
    sleep = FakeSleep()
    sent = await make_notifier(bot, sleep).send_ads([make_ad(1)], TITLES)
    assert sent == [1]
    assert len(bot.sent) == 1
    assert 7 in sleep.calls


async def test_persistent_retry_after_gives_up_without_marking_sent() -> None:
    bot = FakeBot([TelegramRetryAfter(METHOD, "flood", 1)] * 3)
    sent = await make_notifier(bot, FakeSleep()).send_ads([make_ad(1)], TITLES)
    # RetryAfter — подкласс TelegramAPIError: отправка прервана, id не возвращён
    assert sent == []


async def test_bad_request_skips_only_that_card() -> None:
    bot = FakeBot([TelegramBadRequest(METHOD, "cannot parse"), None])
    sent = await make_notifier(bot, FakeSleep()).send_ads([make_ad(1), make_ad(2)], TITLES)
    assert sent == [2]


async def test_network_error_stops_batch_and_keeps_rest_unsent() -> None:
    bot = FakeBot([None, TelegramNetworkError(METHOD, "down")])
    sent = await make_notifier(bot, FakeSleep()).send_ads(
        [make_ad(1), make_ad(2), make_ad(3)], TITLES
    )
    assert sent == [1]  # 2 и 3 не помечаются отправленными


async def test_digest_failure_marks_nothing() -> None:
    bot = FakeBot([TelegramNetworkError(METHOD, "down")])
    ads = [make_ad(i) for i in range(1, 10)]
    assert await make_notifier(bot, FakeSleep()).send_ads(ads, TITLES) == []


async def test_foreign_url_ad_is_not_sent_nor_marked() -> None:
    bot = FakeBot()
    bad = make_ad(2, url="https://evil.example/x")
    sent = await make_notifier(bot, FakeSleep()).send_ads([make_ad(1), bad], TITLES)
    assert sent == [1]
    assert len(bot.sent) == 1


async def test_empty_list_sends_nothing() -> None:
    bot = FakeBot()
    assert await make_notifier(bot, FakeSleep()).send_ads([], TITLES, header="h") == []
    assert bot.sent == []
