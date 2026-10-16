"""v1.2: панель /admin, заявки, приглашения, права друзей (Dispatcher + подменённый Telegram)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageOriginHiddenUser,
    MessageOriginUser,
    Update,
    User,
)
from test_filters_menu import Telegram

from jobbot.bot.admin import AdminCB
from jobbot.bot.filters_menu import FilterCB
from jobbot.bot.handlers import BotDeps, build_dispatcher
from jobbot.config import DEFAULT_EXCLUDE, DEFAULT_INCLUDE, Settings
from jobbot.filters import FilterProfile
from jobbot.models import Ad
from jobbot.notifier import Notifier
from jobbot.poller import Poller
from jobbot.storage import Storage

OWNER = 111
FRIEND = 777
OTHER = 888


class FakeFetcher:
    async def fetch_page(self, category_url: str, page_no: int = 1) -> str:
        return "<html></html>"


async def no_sleep(_seconds: float) -> None:
    return None


class TG(Telegram):
    """Как в тестах меню, но помнит адресатов и умеет «не пускать» к тем, кто не писал боту."""

    def __init__(self) -> None:
        super().__init__()
        self.chat_of: dict[int, int] = {}
        self.not_started: set[int] = set()

    async def __call__(self, bot: Bot, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, SendMessage):
            chat_id = int(method.chat_id)
            if chat_id in self.not_started:
                raise TelegramForbiddenError(method, "bot can't initiate conversation")
            result = await super().__call__(bot, method, timeout)
            self.chat_of[result.message_id] = chat_id
            return result
        return await super().__call__(bot, method, timeout)

    def texts_to(self, chat_id: int) -> list[str]:
        return [self.messages[i][0] for i in self.sent_ids if self.chat_of.get(i) == chat_id]

    def last_id_to(self, chat_id: int) -> int:
        return [i for i in self.sent_ids if self.chat_of.get(i) == chat_id][-1]


class Env:
    def __init__(self, dp: Any, bot: Bot, tg: TG, storage: Storage) -> None:
        self.dp, self.bot, self.tg, self.storage = dp, bot, tg, storage
        self._uid = 0

    def uid(self) -> int:
        self._uid += 1
        return self._uid

    async def say(
        self,
        text: str,
        chat_id: int = OWNER,
        *,
        username: str | None = None,
        name: str = "u",
        **extra: Any,
    ) -> None:
        user = User(id=chat_id, is_bot=False, first_name=name, username=username)
        message = Message(
            message_id=self.uid() + 90000,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=user,
            text=text,
            **extra,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self.uid(), message=message))

    async def press(self, data: str, message_id: int, chat_id: int = OWNER) -> None:
        message = Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            text=self.tg.messages.get(message_id, ("",))[0],
        )
        query = CallbackQuery(
            id=str(self.uid()),
            from_user=User(id=chat_id, is_bot=False, first_name="u"),
            chat_instance="c",
            message=message,
            data=data,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self.uid(), callback_query=query))

    def keyboard(self, message_id: int) -> InlineKeyboardMarkup:
        markup = self.tg.messages[message_id][1]
        assert isinstance(markup, InlineKeyboardMarkup), self.tg.messages[message_id][0]
        return markup

    def data_of(self, message_id: int, label: str) -> str:
        found = [
            btn.callback_data
            for row in self.keyboard(message_id).inline_keyboard
            for btn in row
            if label in btn.text
        ]
        assert found, f"нет «{label}»: {self.labels(message_id)}"
        return found[0]  # type: ignore[return-value]

    def labels(self, message_id: int) -> list[str]:
        return [btn.text for row in self.keyboard(message_id).inline_keyboard for btn in row]

    async def click(self, message_id: int, label: str, chat_id: int = OWNER) -> None:
        await self.press(self.data_of(message_id, label), message_id, chat_id)

    async def open_admin(self) -> int:
        await self.say("/admin")
        return self.tg.last_id_to(OWNER)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    tg = TG()
    bot = Bot(token="123456789:" + "A" * 35)
    bot.session.make_request = tg  # type: ignore[method-assign]
    storage = await Storage.open(tmp_path / "jobbot.db")
    settings = Settings(
        _env_file=None,
        bot_token="x",  # noqa: S106
        owner_chat_id=OWNER,
        visible_categories="it,students,restaurants",
    )
    await storage.seed_defaults(settings.categories, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    notifier = Notifier(bot, OWNER, sleep=no_sleep)
    poller = Poller(settings, storage, FakeFetcher(), notifier)  # type: ignore[arg-type]
    dp = build_dispatcher(BotDeps(settings, storage, poller, notifier))
    yield Env(dp, bot, tg, storage)
    await bot.session.close()
    await storage.close()


# ---------- заявки ----------


async def test_stranger_request_and_one_click_add(env: Env) -> None:
    await env.say("/start", FRIEND, username="Ivan_P", name="Иван")
    request_id = env.tg.last_id_to(OWNER)
    assert "Заявка на доступ" in env.tg.texts_to(OWNER)[-1]
    assert "Иван (@ivan_p)" in env.tg.texts_to(OWNER)[-1]
    assert len(env.tg.texts_to(FRIEND)) == 1  # «заявка отправлена»

    await env.click(request_id, "Добавить")
    user = await env.storage.get_user(FRIEND)
    assert user is not None and user.username == "ivan_p" and user.name == "Иван"
    assert "Вам открыли доступ" in env.tg.texts_to(FRIEND)[-1]
    assert "👤 <b>Иван @ivan_p</b>" in env.tg.messages[request_id][0]  # карточка друга
    assert await env.storage.list_requests() == []

    await env.say("/start", FRIEND, username="Ivan_P", name="Иван")
    assert "Привет, Иван" in env.tg.texts_to(FRIEND)[-1]


async def test_repeated_stranger_messages_do_not_spam_owner(env: Env) -> None:
    for _ in range(3):
        await env.say("привет", FRIEND)
    assert sum("Заявка на доступ" in t for t in env.tg.texts_to(OWNER)) == 1
    assert len(env.tg.texts_to(FRIEND)) == 1


async def test_declined_request_is_silent_forever(env: Env) -> None:
    await env.say("/start", FRIEND)
    request_id = env.tg.last_id_to(OWNER)
    await env.click(request_id, "Отклонить")
    assert await env.storage.list_requests() == []
    to_owner, to_friend = len(env.tg.texts_to(OWNER)), len(env.tg.texts_to(FRIEND))
    await env.say("/start", FRIEND)
    assert len(env.tg.texts_to(OWNER)) == to_owner  # владельца больше не беспокоим
    assert len(env.tg.texts_to(FRIEND)) == to_friend  # и человеку не отвечаем
    assert await env.storage.get_user(FRIEND) is None


# ---------- добавление из /admin ----------


async def test_add_by_id(env: Env) -> None:
    panel = await env.open_admin()
    assert "Пока только вы" in env.tg.messages[panel][0]
    await env.click(panel, "Добавить друга")
    await env.say(str(FRIEND))
    assert await env.storage.get_user(FRIEND) is not None
    assert "Вам открыли доступ" in env.tg.texts_to(FRIEND)[-1]
    assert f"ID: <code>{FRIEND}</code>" in env.tg.messages[panel][0]
    assert "получил сообщение" in env.tg.texts_to(OWNER)[-1]


async def test_add_by_id_when_friend_never_started_bot(env: Env) -> None:
    env.tg.not_started.add(FRIEND)
    panel = await env.open_admin()
    await env.click(panel, "Добавить друга")
    await env.say(str(FRIEND))
    assert await env.storage.get_user(FRIEND) is not None
    assert "нажмёт /start" in env.tg.texts_to(OWNER)[-1]


async def test_add_by_username_invite_then_friend_opens_bot(env: Env) -> None:
    panel = await env.open_admin()
    await env.click(panel, "Добавить друга")
    await env.say("@Waiter_Friend")
    assert await env.storage.list_invites() == ["waiter_friend"]
    assert "приглашён" in env.tg.messages[panel][0]

    await env.say("/start", FRIEND, username="waiter_friend", name="Али")
    assert await env.storage.get_user(FRIEND) is not None
    assert await env.storage.list_invites() == []
    assert any("открыл бота и добавлен" in t for t in env.tg.texts_to(OWNER))
    assert "Привет, Али" in env.tg.texts_to(FRIEND)[-1]  # сразу как друг, без заявки


async def test_add_by_username_with_existing_request(env: Env) -> None:
    await env.say("hi", FRIEND, username="ali_tj", name="Али")
    panel = await env.open_admin()
    await env.click(panel, "Добавить друга")
    await env.say("ali_tj")
    user = await env.storage.get_user(FRIEND)
    assert user is not None and user.name == "Али"
    assert await env.storage.list_invites() == []


async def test_add_by_forwarded_message(env: Env) -> None:
    panel = await env.open_admin()
    await env.click(panel, "Добавить друга")
    origin = MessageOriginUser(
        date=datetime.now(UTC),
        sender_user=User(id=FRIEND, is_bot=False, first_name="Зара", username="zara"),
    )
    await env.say("переслано", forward_origin=origin)
    user = await env.storage.get_user(FRIEND)
    assert user is not None and user.username == "zara"


async def test_forward_from_hidden_account_explains(env: Env) -> None:
    panel = await env.open_admin()
    await env.click(panel, "Добавить друга")
    origin = MessageOriginHiddenUser(date=datetime.now(UTC), sender_user_name="Скрытый")
    await env.say("переслано", forward_origin=origin)
    assert "скрыл аккаунт" in env.tg.messages[panel][0]
    assert await env.storage.list_users() == []


@pytest.mark.parametrize("text", ["abc def", "@ab", "-100123", "0", str(OWNER)])
async def test_add_invalid_input(env: Env, text: str) -> None:
    panel = await env.open_admin()
    await env.click(panel, "Добавить друга")
    await env.say(text)
    assert "❌" in env.tg.messages[panel][0]
    assert await env.storage.list_users() == [] and await env.storage.list_invites() == []


# ---------- фильтры друга ----------


async def add_friend(env: Env, chat_id: int = FRIEND, **kw: Any) -> None:
    await env.storage.add_user(chat_id, name="Друг", username="drug", **kw)


async def test_owner_restricts_friend_to_waiters(env: Env) -> None:
    await add_friend(env, profile=FilterProfile(categories=["it", "students", "restaurants"]))
    panel = await env.open_admin()
    await env.click(panel, "Друг")
    await env.click(panel, "Фильтры друга")
    assert "Фильтры: Друг @drug" in env.tg.messages[panel][0]
    assert not any("Ключевые слова" in label for label in env.labels(panel))
    await env.click(panel, "Категории")
    await env.click(panel, "IT, телеком")
    await env.click(panel, "Начало карьеры")
    assert (await env.storage.get_user_profile(FRIEND)).categories == ["restaurants"]
    assert (await env.storage.get_filter_profile()).categories == ["it", "students"]  # ваши целы
    await env.click(panel, "Рестораторы")  # последнюю снять нельзя
    assert (await env.storage.get_user_profile(FRIEND)).categories == ["restaurants"]
    await env.click(panel, "Назад")
    await env.click(panel, "К пользователю")
    assert "Рестораторы, повара, официанты" in env.tg.messages[panel][0]


async def test_friend_without_edit_right_sees_read_only(env: Env) -> None:
    await add_friend(env, profile=FilterProfile(categories=["restaurants"]))
    await env.say("/filters", FRIEND)
    text = env.tg.texts_to(FRIEND)[-1]
    assert "Рестораторы" in text and "настраивает владелец" in text
    assert env.tg.messages[env.tg.last_id_to(FRIEND)][1] is None  # без кнопок


@pytest.mark.parametrize(
    "data",
    [
        FilterCB(a="ca", u=FRIEND).pack(),  # нет права менять
        FilterCB(a="ca", u=0).pack(),  # чужой (владельца) профиль
        FilterCB(a="ca", u=OTHER).pack(),  # профиль другого друга
    ],
)
async def test_friend_cannot_forge_filter_buttons(env: Env, data: str) -> None:
    await add_friend(env, profile=FilterProfile(categories=["restaurants"], cities=["душанбе"]))
    await add_friend(env, OTHER, profile=FilterProfile(cities=["худжанд"]))
    await env.storage.update_profile(lambda p: setattr(p, "cities", ["кулоб"]))
    await env.say("/filters", FRIEND)
    await env.press(data, env.tg.last_id_to(FRIEND), chat_id=FRIEND)
    assert env.tg.answers[-1][1] is True  # отказ
    assert (await env.storage.get_user_profile(FRIEND)).cities == ["душанбе"]
    assert (await env.storage.get_user_profile(OTHER)).cities == ["худжанд"]
    assert (await env.storage.get_filter_profile()).cities == ["кулоб"]


async def test_friend_with_edit_right_changes_city_but_not_categories(env: Env) -> None:
    await add_friend(env, profile=FilterProfile(categories=["restaurants"]))
    await env.storage.set_user_can_edit(FRIEND, True)
    await env.say("/filters", FRIEND)
    menu = env.tg.last_id_to(FRIEND)
    assert "Категории" not in " ".join(env.labels(menu))
    assert "Категории настраивает владелец" in env.tg.messages[menu][0]
    await env.click(menu, "Город", FRIEND)
    await env.click(menu, "Душанбе", FRIEND)
    assert (await env.storage.get_user_profile(FRIEND)).cities == ["душанбе"]
    await env.press(FilterCB(a="ct", h="it", u=FRIEND).pack(), menu, chat_id=FRIEND)
    assert env.tg.answers[-1][1] is True
    assert (await env.storage.get_user_profile(FRIEND)).categories == ["restaurants"]


async def test_friend_cannot_use_admin_or_owner_commands(env: Env) -> None:
    await add_friend(env)
    await env.say("/admin", FRIEND)
    await env.say("/include rust", FRIEND)
    assert all("только владельцу" in t for t in env.tg.texts_to(FRIEND))
    assert "rust" not in await env.storage.get_keywords("include")
    await env.press(AdminCB(a="dly", u=FRIEND).pack(), 1, chat_id=FRIEND)
    assert await env.storage.get_user(FRIEND) is not None


# ---------- карточка друга: права, пауза, удаление ----------


async def test_card_toggles_pause_and_delete(env: Env) -> None:
    await add_friend(env)
    panel = await env.open_admin()
    await env.click(panel, "Друг")
    await env.click(panel, "Сам меняет фильтры")
    assert (await env.storage.get_user(FRIEND)).can_edit is True  # type: ignore[union-attr]
    await env.click(panel, "Пауза")
    assert (await env.storage.get_user(FRIEND)).status == "paused"  # type: ignore[union-attr]
    await env.click(panel, "Возобновить")
    assert (await env.storage.get_user(FRIEND)).status == "active"  # type: ignore[union-attr]
    await env.click(panel, "Удалить")
    assert "Удалить" in env.tg.messages[panel][0]
    await env.click(panel, "Отмена")
    assert await env.storage.get_user(FRIEND) is not None
    await env.click(panel, "Удалить")
    await env.click(panel, "Да, удалить")
    assert await env.storage.get_user(FRIEND) is None
    assert "Пока только вы" in env.tg.messages[panel][0]


async def test_cancel_invite(env: Env) -> None:
    await env.storage.add_invite("someone")
    panel = await env.open_admin()
    await env.click(panel, "@someone")
    await env.click(panel, "Отменить приглашение")
    assert await env.storage.list_invites() == []


async def test_forged_admin_callbacks(env: Env) -> None:
    panel = await env.open_admin()
    for data in (AdminCB(a="zzz").pack(), AdminCB(a="rqa", u=424242).pack(), "adm", "adm:x:y:z:w"):
        await env.press(data, panel)
        assert env.tg.answers[-1][1] is True
    assert await env.storage.list_users() == []


# ---------- команды друга ----------


async def test_friend_commands(env: Env) -> None:
    await add_friend(env, profile=FilterProfile(categories=["restaurants"]))
    ads = [Ad(i, f"https://somon.tj/adv/{i}_x/", f"Официант {i}", "restaurants") for i in (1, 2)]
    ads.append(Ad(3, "https://somon.tj/adv/3_x/", "Python", "it"))
    await env.storage.add_ads([(ad, True) for ad in ads], skip_sending=True)

    await env.say("/last", FRIEND)
    cards = [t for t in env.tg.texts_to(FRIEND) if t.startswith("💼")]
    assert len(cards) == 2 and all("Официант" in t for t in cards)

    await env.say("/pause", FRIEND)
    assert (await env.storage.get_user(FRIEND)).status == "paused"  # type: ignore[union-attr]
    await env.say("/status", FRIEND)
    assert "на паузе" in env.tg.texts_to(FRIEND)[-1]
    await env.say("/resume", FRIEND)
    assert (await env.storage.get_user(FRIEND)).status == "active"  # type: ignore[union-attr]
    await env.say("просто текст", FRIEND)
    assert "/help" in env.tg.texts_to(FRIEND)[-1]


async def test_owner_status_shows_friends(env: Env) -> None:
    await add_friend(env)
    await env.say("/status")
    assert "Друзей: 1" in env.tg.texts_to(OWNER)[-1]
