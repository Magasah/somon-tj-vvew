"""Сообщения в Telegram: карточка вакансии, дайджест, отправка с паузами и повтором при лимитах.

Всё, что пришло с сайта (название, зарплата, город, ссылка), — недоверенные данные,
поэтому каждое значение проходит через `html.escape` до вставки в HTML-сообщение.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from jobbot.config import BASE_URL
from jobbot.models import Ad

log = logging.getLogger("notifier")

AD_URL_PREFIX = f"{BASE_URL}/adv/"
BUTTON_TEXT = "Открыть на Somon"
SIMPLIFIED_MARK = "⚠️ (упрощённый режим)"
UNCHECKED_MARK = "⚠️ график/стаж не проверены"

# Лимит Telegram — 4096 символов на сообщение; берём с запасом.
MAX_MESSAGE_LEN = 4000
MIN_SEND_INTERVAL_SEC = 1.0  # пауза между сообщениями
MAX_RETRY_AFTER_ATTEMPTS = 3

Sleep = Callable[[float], Awaitable[None]]


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def is_safe_ad_url(url: str) -> bool:
    """Ссылка ведёт на объявление somon.tj (защита от подмены данными с сайта)."""
    return url.startswith(AD_URL_PREFIX)


def format_card(ad: Ad, category_title: str, *, simplified: bool = False) -> str:
    """Текст карточки вакансии (HTML). Строки без данных пропускаются."""
    lines = [f"💼 <b>{_esc(ad.title)}</b>"]
    details = [
        f"{icon} {_esc(value)}"
        for icon, value in (("💰", ad.salary_text), ("📍", ad.city), ("🕒", ad.date_label))
        if value
    ]
    if details:
        lines.append(" · ".join(details))
    lines.append(f"🏷 {_esc(category_title)}")
    if ad.details_unchecked:
        lines.append(UNCHECKED_MARK)
    if simplified:
        lines.append(SIMPLIFIED_MARK)
    return "\n".join(lines)


def card_keyboard(ad: Ad) -> InlineKeyboardMarkup | None:
    """Кнопка «Открыть на Somon»; без неё остаёмся, если ссылка не прошла проверку."""
    if not is_safe_ad_url(ad.url):
        return None
    button = InlineKeyboardButton(text=BUTTON_TEXT, url=ad.url)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def format_seed_header(category_title: str) -> str:
    return f"📌 <b>Последние вакансии в разделе «{_esc(category_title)}»</b>"


def _plural_vacancies(count: int) -> str:
    if count % 100 in range(11, 15):
        return "новых вакансий"
    match count % 10:
        case 1:
            return "новая вакансия"
        case 2 | 3 | 4:
            return "новые вакансии"
        case _:
            return "новых вакансий"


def format_digest(
    ads: Sequence[Ad], *, simplified: bool = False, limit: int = MAX_MESSAGE_LEN
) -> list[str]:
    """Дайджест: «Найдено N новых вакансий» и строки «• название — ссылка».

    Если текст длиннее лимита Telegram, он делится на несколько сообщений по границам строк.
    """
    header = f"🔔 <b>Найдено {len(ads)} {_plural_vacancies(len(ads))}</b>"
    if simplified:
        header += f"\n{SIMPLIFIED_MARK}"
    lines = [f"• {_esc(ad.title)} — {_esc(ad.url)}" for ad in ads if is_safe_ad_url(ad.url)]

    messages: list[str] = []
    current = header
    for line in lines:
        if len(current) + 1 + len(line) > limit:
            messages.append(current)
            current = line
        else:
            current = f"{current}\n{line}"
    messages.append(current)
    return messages


class Notifier:
    """Отправляет сообщения владельцу. Пауза между сообщениями — не меньше секунды."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        digest_threshold: int = 8,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._threshold = digest_threshold
        self._sleep = sleep
        self._last_sent_at: float | None = None

    async def send_text(
        self,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        preview: bool = False,
    ) -> None:
        """Отправить одно сообщение (HTML). Ждёт паузу, при `TelegramRetryAfter` повторяет."""
        for attempt in range(1, MAX_RETRY_AFTER_ATTEMPTS + 1):
            await self._pause()
            try:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    link_preview_options=LinkPreviewOptions(is_disabled=not preview),
                )
            except TelegramRetryAfter as exc:
                self._last_sent_at = time.monotonic()
                if attempt == MAX_RETRY_AFTER_ATTEMPTS:
                    raise
                log.warning("Telegram просит подождать %d с", exc.retry_after)
                await self._sleep(exc.retry_after)
            else:
                self._last_sent_at = time.monotonic()
                return

    async def send_ads(
        self,
        ads: Sequence[Ad],
        category_titles: Mapping[str, str],
        *,
        header: str | None = None,
        simplified: bool = False,
    ) -> list[int]:
        """Отправить объявления и вернуть `ad_id` тех, что точно доставлены.

        Больше `digest_threshold` штук → дайджест, иначе отдельные карточки.
        Вызывающий помечает `sent_at` только для возвращённых id, остальные останутся
        неотправленными и будут досланы позже (раздел 4.2 ТЗ).
        `header` — необязательный заголовок («Последние вакансии в разделе …»): он добавляется
        сверху первого сообщения, отдельным сообщением не уходит (значит, число сообщений
        равно числу карточек).
        """
        if not ads:
            return []
        if len(ads) > self._threshold:
            return await self._send_digest(ads, simplified, header)
        return await self._send_cards(ads, category_titles, simplified, header)

    async def send_digest(self, ads: Sequence[Ad]) -> list[int]:
        """Отправить объявления одним списком (дайджестом) независимо от их числа."""
        if not ads:
            return []
        return await self._send_digest(ads, False, None)

    async def _send_digest(
        self, ads: Sequence[Ad], simplified: bool, header: str | None
    ) -> list[int]:
        try:
            for text in format_digest(ads, simplified=simplified):
                if header is not None:
                    text, header = f"{header}\n\n{text}", None
                await self.send_text(text)
        except TelegramAPIError as exc:
            # Часть сообщений могла уйти — не знаем какая, поэтому не помечаем ничего:
            # лучше повтор, чем потеря.
            log.error("не удалось отправить дайджест: %s", exc)
            return []
        return [ad.ad_id for ad in ads if is_safe_ad_url(ad.url)]

    async def _send_cards(
        self,
        ads: Sequence[Ad],
        category_titles: Mapping[str, str],
        simplified: bool,
        header: str | None,
    ) -> list[int]:
        sent: list[int] = []
        for ad in ads:
            if not is_safe_ad_url(ad.url):
                log.warning("объявление %d пропущено: подозрительная ссылка", ad.ad_id)
                continue
            title = category_titles.get(ad.category_key, ad.category_key)
            text = format_card(ad, title, simplified=simplified)
            if header is not None:
                text = f"{header}\n\n{text}"
            try:
                await self.send_text(text, reply_markup=card_keyboard(ad))
            except TelegramBadRequest as exc:
                log.error("объявление %d отклонено Telegram: %s", ad.ad_id, exc)
                continue  # проблема в этом сообщении — остальные отправляем
            except TelegramAPIError as exc:
                log.error("отправка прервана (сеть/доступ): %s", exc)
                break  # дальше пробовать бессмысленно, остаток досылается позже
            header = None  # заголовок доставлен вместе с первой карточкой
            sent.append(ad.ad_id)
        return sent

    async def _pause(self) -> None:
        if self._last_sent_at is None:
            return
        wait = MIN_SEND_INTERVAL_SEC - (time.monotonic() - self._last_sent_at)
        if wait > 0:
            await self._sleep(wait)
