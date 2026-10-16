"""Цикл опроса: загрузка разделов, поиск новых объявлений, фильтр, отправка владельцу.

Порядок для каждого нового объявления (раздел 4.2 ТЗ), чтобы не было ни дублей, ни потерь:
1. в одной транзакции `INSERT OR IGNORE` с `sent_at = NULL`; строка уже была — объявление не новое;
2. отправка в Telegram;
3. только после успешной отправки `UPDATE ads SET sent_at = ...`;
4. при старте досылаются подходящие объявления с `sent_at IS NULL` не старше 24 часов.
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aiogram.exceptions import TelegramAPIError

from jobbot.config import MIN_POLL_INTERVAL_SEC, Category, Settings
from jobbot.fetcher import (
    BlockedError,
    Fetcher,
    FetchError,
    HttpStatusError,
    RobotsDisallowedError,
)
from jobbot.filters import FilterProfile
from jobbot.matcher import Mode
from jobbot.models import Ad
from jobbot.notifier import Notifier, format_seed_header
from jobbot.parser import parse_detail, parse_page
from jobbot.selection import CardFacts, Decision, check_card, check_details
from jobbot.storage import OWNER_ID, Storage, from_iso, to_iso

log = logging.getLogger("poller")

UNSENT_WINDOW = timedelta(hours=24)  # насколько старые неотправленные досылаем при старте
BLOCK_PAUSE = timedelta(minutes=30)  # пауза после 403/429 (раздел 2.3 ТЗ)

STATE_PAUSED = "paused"
STATE_LAST_POLL = "last_poll_at"
STATE_LAST_ERROR = "last_error"
STATE_PAUSED_AT = "paused_at"
STATE_BLOCKED_UNTIL = "blocked_until"
STATE_BLOCK_ALERTED = "block_alerted"
STATE_FAIL_STREAK = "fetch_fail_streak"
STATE_NET_ALERTED = "net_alerted"

# Самодиагностика (раздел 3.6 ТЗ)
EMPTY_STREAK_LIMIT = 3  # столько циклов подряд раздел без объявлений → предупреждение
EMPTY_ALERT_INTERVAL = timedelta(hours=6)  # повторять не чаще
NET_FAIL_LIMIT = 5  # столько сетевых ошибок подряд → одно уведомление
ROBOTS_ALERT_INTERVAL = timedelta(hours=24)


@dataclass(frozen=True)
class Recipient:
    """Кому раздаются вакансии: владелец (chat_id = 0) или друг (v1.2)."""

    chat_id: int
    profile: FilterProfile
    modes: Mapping[str, Mode]  # режим ключевых слов по разделам (только у владельца)
    include: list[str]
    exclude: list[str]
    paused: bool = False

    def decide(self, ad: Ad) -> Decision:
        return check_card(
            CardFacts.from_ad(ad),
            self.profile,
            keyword_mode=self.modes.get(ad.category_key, "all"),
            include=self.include,
            exclude=self.exclude,
        )


@dataclass
class CategoryStats:
    """Итог по одному разделу за цикл (для лога и /status)."""

    key: str
    found: int = 0
    new: int = 0
    sent: int = 0
    error: str | None = None


@dataclass
class CycleStats:
    categories: list[CategoryStats] = field(default_factory=list)
    skipped: str | None = None  # причина, если цикл не выполнялся (пауза после 403/429)
    details_loaded: int = 0  # v1.1: сколько страниц объявлений загружено за цикл


class Poller:
    """Один опрос = `poll_once()`. `run_forever()` повторяет его с интервалом и джиттером."""

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        fetcher: Fetcher,
        notifier: Notifier,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._fetcher = fetcher
        self._notifier = notifier
        self._now = now
        self._rng = rng or random.Random()  # noqa: S311 — джиттер, не криптография
        self._lock = asyncio.Lock()  # один опрос за раз: цикл и /check не пересекаются
        self._titles = {c.key: c.title for c in settings.categories}

    @property
    def busy(self) -> bool:
        """Идёт ли опрос прямо сейчас."""
        return self._lock.locked()

    # ---------- главный цикл ----------

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Опрашивать, пока не установлен `stop`. Исключение в цикле не останавливает работу."""
        try:
            await self.flush_unsent()
        except Exception:
            log.exception("не удалось доотправить накопленные объявления")
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception:
                log.exception("ошибка в цикле опроса")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.next_delay())
            except TimeoutError:
                pass

    def next_delay(self) -> float:
        """Секунды до следующего опроса: интервал ± джиттер, но не меньше 3 минут."""
        jitter = self._settings.poll_jitter_sec
        delay = self._settings.poll_interval_sec + self._rng.uniform(-jitter, jitter)
        return max(delay, MIN_POLL_INTERVAL_SEC)

    async def poll_once(self) -> CycleStats | None:
        """Один опрос всех включённых разделов. None — опрос уже идёт (второй не запускаем)."""
        if self._lock.locked():
            return None
        async with self._lock:
            return await self._poll_cycle()

    # ---------- досылка ----------

    async def flush_unsent(self, since: datetime | None = None) -> int:
        """Отправить подходящие объявления с `sent_at IS NULL` (старт бота, /resume).

        По умолчанию — найденные не раньше чем 24 часа назад. Возвращает число отправленных.
        """
        return await self._send_pending(since=since)

    # ---------- один цикл ----------

    async def _recipients(self) -> list[Recipient]:
        """Кому раздаём вакансии: владелец + друзья (кроме заблокировавших бота)."""
        owner = Recipient(
            chat_id=OWNER_ID,
            profile=await self._storage.get_filter_profile(),
            modes={s.key: s.mode for s in await self._storage.get_categories()},
            include=await self._storage.get_keywords("include"),
            exclude=await self._storage.get_keywords("exclude"),
            paused=await self._is_paused(),
        )
        friends = [
            Recipient(
                chat_id=user.chat_id,
                profile=user.profile,
                modes={},  # у друзей нет ключевых слов: только фильтры профиля
                include=[],
                exclude=[],
                paused=user.status == "paused",
            )
            for user in await self._storage.list_users()
            if user.status != "blocked"
        ]
        return [owner, *friends]

    async def _poll_cycle(self) -> CycleStats:
        stats = CycleStats()
        blocked_until = await self._storage.get_state(STATE_BLOCKED_UNTIL)
        if blocked_until and blocked_until > to_iso(self._now()):
            log.warning("опрос пропущен: пауза после 403/429 до %s", blocked_until)
            stats.skipped = "blocked"
            return stats

        recipients = await self._recipients()
        simplified_ids: set[int] = set()  # найдены запасным парсером → пометка в сообщении
        # Опрашиваем только видимые разделы (VISIBLE_CATEGORIES), выбранные хоть у кого-то.
        wanted = {key for r in recipients for key in r.profile.categories}
        selected = [c for c in self._settings.categories if c.key in wanted]
        if not selected:
            log.warning("ни один выбранный в фильтрах раздел не входит в VISIBLE_CATEGORIES")
        blocked = False

        for category in selected:
            cat_stats = CategoryStats(category.key)
            stats.categories.append(cat_stats)
            try:
                await self._poll_category(category, recipients, cat_stats, simplified_ids)
            except BlockedError as exc:
                cat_stats.error = str(exc)
                await self._enter_block_pause(exc)
                blocked = True
                break  # остальные разделы не трогаем
            except RobotsDisallowedError as exc:
                cat_stats.error = str(exc)
                log.error("%s: %s", category.key, exc)
                await self._record_error(f"{category.key}: {exc}")
                await self._alert(
                    f"robots:{category.key}",
                    f"🤖 robots.txt сайта запрещает раздел «{html.escape(category.title)}» — "
                    "запросы к нему не отправляются.",
                    min_interval=ROBOTS_ALERT_INTERVAL,
                )
            except FetchError as exc:
                cat_stats.error = str(exc)
                log.error("%s: ошибка загрузки: %s", category.key, exc)
                await self._record_error(f"{category.key}: {exc}")
                await self._note_fetch_failed(exc)
            except Exception as exc:
                cat_stats.error = repr(exc)
                log.exception("%s: ошибка при обработке раздела", category.key)
                await self._record_error(f"{category.key}: {exc!r}")

        if not blocked:
            try:
                await self._process_details(recipients, stats)
            except Exception:
                log.exception("ошибка в очереди страниц объявлений")

        await self._send_pending(stats=stats, simplified_ids=simplified_ids)
        try:
            await self._send_friends(recipients, stats, simplified_ids)
        except Exception:
            log.exception("ошибка при отправке друзьям")
        await self._storage.set_state(STATE_LAST_POLL, to_iso(self._now()))
        if stats.details_loaded:
            log.info("страниц объявлений загружено: %d", stats.details_loaded)
        for cat_stats in stats.categories:
            log.info(
                "%s: найдено %d, новых %d, отправлено %d",
                cat_stats.key,
                cat_stats.found,
                cat_stats.new,
                cat_stats.sent,
            )
        return stats

    async def _poll_category(
        self,
        category: Category,
        recipients: list[Recipient],
        stats: CategoryStats,
        simplified_ids: set[int],
    ) -> None:
        # Первый запуск раздела: в базе пусто → всё текущее сохраняем как уже просмотренное.
        seeding = not await self._storage.has_ads(category.key)
        seed_pools: dict[int, list[Ad]] = {r.chat_id: [] for r in recipients}

        for page in range(1, self._settings.max_pages + 1):
            page_html = await self._fetcher.fetch_page(category.url, page)
            await self._note_fetch_ok()
            result = parse_page(page_html, category.key)
            if page == 1:
                await self._track_empty(category, len(result.ads))
            if not result.ads:
                break
            stats.found += len(result.ads)

            # Ступень 1 отбора — по карточке, без запросов к сайту, для каждого получателя.
            decisions = {
                ad.ad_id: {r.chat_id: r.decide(ad) for r in recipients} for ad in result.ads
            }
            items = [(ad, decisions[ad.ad_id][OWNER_ID] is Decision.ACCEPT) for ad in result.ads]
            # При первом запуске страницы объявлений не грузим: это «тихое» запоминание.
            statuses = {
                ad_id: (
                    "pending"
                    if not seeding and Decision.NEED_DETAILS in per.values()
                    else "skipped"
                )
                for ad_id, per in decisions.items()
            }
            new = await self._storage.add_ads(
                items, skip_sending=seeding, details_status=statuses, now=self._now()
            )
            stats.new += len(new)
            await self._storage.record_attr_values("city", [ad.city for ad in new], now=self._now())
            rows: list[tuple[int, int, str]] = []
            for ad in new:
                for chat_id, decision in decisions[ad.ad_id].items():
                    if decision is Decision.REJECT:
                        continue
                    if seeding:
                        seed_pools[chat_id].append(ad)
                    elif decision is Decision.NEED_DETAILS:
                        rows.append((ad.ad_id, chat_id, "waiting"))
                    elif chat_id != OWNER_ID:
                        rows.append((ad.ad_id, chat_id, "ready"))
                if result.simplified and not seeding:
                    simplified_ids.add(ad.ad_id)
            await self._storage.add_deliveries(rows, now=self._now())

            # Дальше листаем, только если на странице всё новое (бот долго был выключен).
            if len(new) < len(result.ads):
                break

        if seeding:
            by_id = {r.chat_id: r for r in recipients}
            for chat_id, pool in seed_pools.items():
                if pool and not by_id[chat_id].paused:
                    await self._send_seed(category, pool, stats, chat_id)

    async def _process_details(self, recipients: list[Recipient], stats: CycleStats) -> None:
        """Ступень 2: страницы объявлений из очереди (новые первыми, не больше лимита за цикл).

        Ошибка загрузки → ещё одна попытка в следующем цикле; после 3 неудач объявление
        отправляется с пометкой «график/стаж не проверены». Удалённое объявление (404/410)
        не отправляется. Решение принимается отдельно для каждого, кто ждал страницу.
        """
        now = self._now()
        await self._storage.expire_pending_details(now - UNSENT_WINDOW)
        for recipient in recipients:
            if not recipient.profile.needs_details:
                released = await self._storage.release_waiting(recipient.chat_id)
                if released:
                    log.info("график/стаж сняты с фильтра: %d объявлений подходят", released)
        if not any(r.profile.needs_details for r in recipients):
            return

        profiles = {r.chat_id: r.profile for r in recipients}
        queue = await self._storage.pending_details(
            now - UNSENT_WINDOW, self._settings.max_details_per_cycle
        )
        for item in queue:
            ad = item.ad
            try:
                page_html = await self._fetcher.fetch_detail(ad.url)
            except BlockedError as exc:
                await self._enter_block_pause(exc)
                return
            except HttpStatusError as exc:
                if exc.status_code in (404, 410):
                    log.info("объявление %d удалено с сайта — не отправляем", ad.ad_id)
                    await self._storage.drop_pending_detail(ad.ad_id)
                else:
                    attempts = await self._storage.detail_failed(ad.ad_id)
                    log.warning("страница %d: %s (попытка %d)", ad.ad_id, exc, attempts)
                continue
            except (RobotsDisallowedError, ValueError) as exc:
                # Адрес нельзя запрашивать (robots.txt, `---` в ссылке) — повторять бесполезно.
                log.warning("страница %d недоступна для бота: %s", ad.ad_id, exc)
                await self._storage.detail_failed(ad.ad_id, permanent=True)
                continue
            except FetchError as exc:
                await self._note_fetch_failed(exc)
                attempts = await self._storage.detail_failed(ad.ad_id)
                log.warning("страница %d не загрузилась: %s (попытка %d)", ad.ad_id, exc, attempts)
                continue

            await self._note_fetch_ok()
            details = parse_detail(page_html)
            await self._storage.record_attr_values("schedule", [details.schedule], now=now)
            await self._storage.record_attr_values("experience", [details.experience], now=now)
            decisions = {
                chat_id: chat_id in profiles and check_details(details, profiles[chat_id])
                for chat_id in await self._storage.waiting_for(ad.ad_id)
            }
            await self._storage.resolve_details(ad.ad_id, details, decisions)
            stats.details_loaded += 1

    async def _send_seed(
        self, category: Category, pool: list[Ad], stats: CategoryStats, chat_id: int = OWNER_ID
    ) -> None:
        """Первый запуск раздела: показать получателю только последние SEED_SEND_LAST вакансий."""
        count = self._settings.seed_send_last
        if count <= 0:
            return
        newest = sorted(pool, key=lambda ad: ad.ad_id, reverse=True)[:count]  # больший id = свежее
        sent = await self._notifier.send_ads(
            newest,
            self._titles,
            header=format_seed_header(category.title),
            chat_id=None if chat_id == OWNER_ID else chat_id,
        )
        if chat_id == OWNER_ID:
            await self._storage.mark_sent(sent, now=self._now())  # для счётчика в /status
        else:
            await self._storage.mark_delivered(chat_id, sent, now=self._now())
        stats.sent += len(sent)

    async def _send_pending(
        self,
        *,
        since: datetime | None = None,
        stats: CycleStats | None = None,
        simplified_ids: set[int] | None = None,
    ) -> int:
        """Владельцу: отправить всё подходящее с `sent_at IS NULL` и пометить доставленное.

        Новые объявления цикла уже лежат в базе неотправленными (шаг 1 из раздела 4.2),
        поэтому заодно уходит то, что не удалось отправить раньше (сбой сети и т.п.).
        """
        if await self._is_paused():
            return 0
        pending = await self._storage.unsent_matched(since or self._now() - UNSENT_WINDOW)
        if not pending:
            return 0
        by_key = {s.key: s for s in stats.categories} if stats else {}
        simplified_ids = simplified_ids or set()
        total = 0
        # Объявления запасного парсера идут отдельным сообщением с пометкой «упрощённый режим».
        for simplified in (False, True):
            group = [ad for ad in pending if (ad.ad_id in simplified_ids) is simplified]
            if not group:
                continue
            sent = await self._notifier.send_ads(group, self._titles, simplified=simplified)
            await self._storage.mark_sent(sent, now=self._now())
            total += len(sent)
            sent_set = set(sent)
            for ad in group:
                if ad.ad_id in sent_set and ad.category_key in by_key:
                    by_key[ad.category_key].sent += 1
        return total

    async def _send_friends(
        self,
        recipients: list[Recipient],
        stats: CycleStats | None = None,
        simplified_ids: set[int] | None = None,
    ) -> int:
        """Друзьям: каждому — подходящие ему и ещё не отправленные вакансии."""
        total = 0
        for recipient in recipients:
            if recipient.chat_id == OWNER_ID or recipient.paused:
                continue
            total += await self.flush_friend(
                recipient.chat_id, stats=stats, simplified_ids=simplified_ids
            )
        return total

    async def flush_friend(
        self,
        chat_id: int,
        since: datetime | None = None,
        *,
        stats: CycleStats | None = None,
        simplified_ids: set[int] | None = None,
    ) -> int:
        """Отправить другу накопленное (цикл опроса, /resume). Возвращает число отправленных."""
        ads = await self._storage.ready_for(chat_id, since or self._now() - UNSENT_WINDOW)
        if not ads:
            return 0
        by_key = {s.key: s for s in stats.categories} if stats else {}
        simplified_ids = simplified_ids or set()
        total = 0
        for simplified in (False, True):
            group = [ad for ad in ads if (ad.ad_id in simplified_ids) is simplified]
            if not group:
                continue
            sent = await self._notifier.send_ads(
                group, self._titles, simplified=simplified, chat_id=chat_id
            )
            await self._storage.mark_delivered(chat_id, sent, now=self._now())
            total += len(sent)
            for ad in group:
                if ad.ad_id in set(sent) and ad.category_key in by_key:
                    by_key[ad.category_key].sent += 1
        if chat_id in self._notifier.forbidden:
            await self._friend_blocked_bot(chat_id)
        return total

    async def _friend_blocked_bot(self, chat_id: int) -> None:
        """Друг заблокировал бота: больше не пытаемся писать, владельцу — одно сообщение."""
        self._notifier.forbidden.discard(chat_id)
        user = await self._storage.get_user(chat_id)
        await self._storage.set_user_status(chat_id, "blocked")
        name = html.escape(user.title if user else str(chat_id))
        log.warning("пользователь %d заблокировал бота — отправка остановлена", chat_id)
        await self._alert(
            f"blocked_user:{chat_id}",
            f"🚫 {name} заблокировал бота — вакансии ему больше не отправляются. "
            "Включить снова: /admin → пользователь → «Возобновить».",
        )

    # ---------- служебное ----------

    async def _is_paused(self) -> bool:
        return await self._storage.get_state(STATE_PAUSED) == "1"

    async def _record_error(self, text: str) -> None:
        await self._storage.set_state(STATE_LAST_ERROR, f"{to_iso(self._now())} {text}")

    async def _enter_block_pause(self, exc: BlockedError) -> None:
        until = to_iso(self._now() + BLOCK_PAUSE)
        log.error("сайт ответил %d — пауза 30 минут (до %s)", exc.status_code, until)
        await self._storage.set_state(STATE_BLOCKED_UNTIL, until)
        await self._record_error(str(exc))
        # Одно уведомление на «эпизод» блокировки: флаг снимается после первой удачной загрузки.
        if await self._storage.get_state(STATE_BLOCK_ALERTED) != "1":
            sent = await self._alert(
                "blocked",
                f"🚫 Сайт ответил {exc.status_code} (ограничение доступа). "
                "Опрос приостановлен на 30 минут.",
            )
            if sent:
                await self._storage.set_state(STATE_BLOCK_ALERTED, "1")

    # ---------- самодиагностика (раздел 3.6 ТЗ) ----------

    async def _track_empty(self, category: Category, found: int) -> None:
        """Раздел без объявлений 3 цикла подряд → предупреждение (не чаще раза в 6 часов)."""
        key = f"empty_streak:{category.key}"
        if found > 0:
            if await self._storage.get_state(key) not in (None, "0"):
                await self._storage.set_state(key, "0")
            return
        streak = int(await self._storage.get_state(key, "0") or 0) + 1
        await self._storage.set_state(key, str(streak))
        log.warning("%s: на странице 0 объявлений (подряд: %d)", category.key, streak)
        if streak >= EMPTY_STREAK_LIMIT:
            await self._alert(
                f"empty:{category.key}",
                f"⚠️ Парсер раздела «{html.escape(category.title)}» не нашёл объявлений "
                f"{EMPTY_STREAK_LIMIT} раза подряд. Возможно, сайт изменил вёрстку.",
                min_interval=EMPTY_ALERT_INTERVAL,
            )

    async def _note_fetch_failed(self, exc: FetchError) -> None:
        """Сетевая ошибка: после 5 подряд — одно уведомление."""
        streak = int(await self._storage.get_state(STATE_FAIL_STREAK, "0") or 0) + 1
        await self._storage.set_state(STATE_FAIL_STREAK, str(streak))
        if streak >= NET_FAIL_LIMIT and await self._storage.get_state(STATE_NET_ALERTED) != "1":
            sent = await self._alert(
                "network",
                f"⚠️ Не удаётся связаться с somon.tj: {streak} ошибок подряд.\n"
                f"Последняя: {html.escape(str(exc))}",
            )
            if sent:
                await self._storage.set_state(STATE_NET_ALERTED, "1")

    async def _note_fetch_ok(self) -> None:
        """Страница загрузилась: сбросить счётчики ошибок, сообщить о восстановлении связи."""
        if await self._storage.get_state(STATE_FAIL_STREAK, "0") != "0":
            await self._storage.set_state(STATE_FAIL_STREAK, "0")
        if await self._storage.get_state(STATE_NET_ALERTED) == "1":
            await self._storage.delete_state(STATE_NET_ALERTED)
            await self._alert("recovered", "✅ Связь с сайтом восстановлена.")
        if await self._storage.get_state(STATE_BLOCK_ALERTED) == "1":
            await self._storage.delete_state(STATE_BLOCK_ALERTED)
        if await self._storage.get_state(STATE_BLOCKED_UNTIL) is not None:
            await self._storage.delete_state(STATE_BLOCKED_UNTIL)

    async def _alert(self, kind: str, text: str, *, min_interval: timedelta | None = None) -> bool:
        """Служебное уведомление владельцу (работает и на паузе). True — доставлено.

        min_interval — не чаще одного такого уведомления за период (`last_alert_at:<kind>`).
        Ошибка отправки не должна ломать опрос.
        """
        state_key = f"last_alert_at:{kind}"
        if min_interval is not None:
            last = await self._storage.get_state(state_key)
            if last and from_iso(last) + min_interval > self._now():
                return False
        try:
            await self._notifier.send_text(text)
        except TelegramAPIError as exc:
            log.error("не удалось отправить уведомление %s: %s", kind, exc)
            return False
        await self._storage.set_state(state_key, to_iso(self._now()))
        return True
