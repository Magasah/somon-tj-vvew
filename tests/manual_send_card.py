"""Ручная проверка этапа 3: отправить владельцу тестовые карточки в Telegram.

Нужны BOT_TOKEN и OWNER_CHAT_ID в .env. Pytest этот файл не запускает (имя без `test_`).
Запуск из корня проекта:  python tests/manual_send_card.py
Придут 2 сообщения: реальная вакансия из фикстуры (с заголовком «Последние вакансии…» сверху)
и карточка с опасным названием (`<script>` должен показаться как обычный текст).
"""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiogram import Bot  # noqa: E402

from jobbot.config import DEFAULT_CATEGORIES, load_settings  # noqa: E402
from jobbot.notifier import Notifier, format_seed_header  # noqa: E402
from jobbot.parser import parse_listing  # noqa: E402


async def main() -> None:
    settings = load_settings()
    html = (ROOT / "tests" / "fixtures" / "it_page1.html").read_text(encoding="utf-8")
    real_ad = parse_listing(html, "it")[0]
    evil_ad = replace(real_ad, ad_id=1, title="<script>alert(1)</script> & <b>тест</b>")
    titles = {c.key: c.title for c in DEFAULT_CATEGORIES}

    bot = Bot(token=settings.bot_token.get_secret_value())
    try:
        notifier = Notifier(bot, settings.owner_chat_id or 0)
        sent = await notifier.send_ads(
            [real_ad, evil_ad], titles, header=format_seed_header(titles["it"])
        )
        print(f"Отправлено карточек: {len(sent)} из 2. Проверьте Telegram.")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
