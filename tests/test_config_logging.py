"""Тесты каркаса: валидация настроек и маскировка токена в логах."""

import logging

import pytest

from jobbot.config import Settings, load_settings
from jobbot.logging_setup import MASK, SafeFormatter, mask_secrets

FAKE_TOKEN = "123456789:" + "A" * 35


def test_mask_secrets_hides_token() -> None:
    assert FAKE_TOKEN not in mask_secrets(f"url=/bot{FAKE_TOKEN}/getMe")
    assert MASK in mask_secrets(f"url=/bot{FAKE_TOKEN}/getMe")


def test_formatter_masks_token_in_message_and_traceback() -> None:
    formatter = SafeFormatter("Asia/Dushanbe")
    try:
        raise RuntimeError(f"fail {FAKE_TOKEN}")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "t", logging.ERROR, "f", 1, "token %s", (FAKE_TOKEN,), sys.exc_info()
        )
    text = formatter.format(record)
    assert FAKE_TOKEN not in text
    assert "| ERROR | t |" in text


@pytest.mark.parametrize(
    "env",
    [
        {"POLL_INTERVAL_SEC": "100"},
        {"REQUEST_DELAY_SEC": "1"},
        {"MAX_PAGES": "0"},
        {"MAX_PAGES": "6"},
        {"OWNER_CHAT_ID": "abc"},
    ],
)
def test_invalid_settings_exit_with_code_1(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.setenv("BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("OWNER_CHAT_ID", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as exc:
        load_settings()
    assert exc.value.code == 1


def test_missing_token_exits_unless_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "")
    monkeypatch.setenv("OWNER_CHAT_ID", "")
    with pytest.raises(SystemExit):
        load_settings()
    assert load_settings(require_telegram=False).owner_chat_id is None


def test_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.poll_interval_sec == 300
    assert [c.key for c in settings.categories] == ["it", "students"]
