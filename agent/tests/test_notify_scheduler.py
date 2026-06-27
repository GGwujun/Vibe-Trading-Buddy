from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.notify.models import NotifyConfig, PlatformConfig
from src.notify.scheduler import _FIRED_KEYS, _iter_due_pushes, send_due_notifications


CST = timezone(timedelta(hours=8))


def test_iter_due_pushes_respects_platform_and_slot_settings() -> None:
    cfg = NotifyConfig(
        feishu=PlatformConfig(
            enabled=True,
            webhook_url="https://example.test/feishu",
            pre_market_enabled=True,
            pre_market_time="08:50",
        ),
        dingtalk=PlatformConfig(
            enabled=True,
            webhook_url="",
            pre_market_enabled=True,
            pre_market_time="08:50",
        ),
        wechat=PlatformConfig(
            enabled=False,
            webhook_url="https://example.test/wechat",
            pre_market_enabled=True,
            pre_market_time="08:50",
        ),
    )

    due = list(_iter_due_pushes(cfg, datetime(2026, 6, 24, 8, 50, tzinfo=CST)))

    assert [(platform, slot) for platform, slot, _ in due] == [("feishu", "pre_market")]


def test_iter_due_pushes_ignores_invalid_times() -> None:
    cfg = NotifyConfig(
        feishu=PlatformConfig(
            enabled=True,
            webhook_url="https://example.test/feishu",
            pre_market_enabled=True,
            pre_market_time="8:5",
        ),
    )

    assert list(_iter_due_pushes(cfg, datetime(2026, 6, 24, 8, 5, tzinfo=CST))) == []


def test_send_due_notifications_deduplicates_same_day(monkeypatch) -> None:
    _FIRED_KEYS.clear()
    cfg = NotifyConfig(
        feishu=PlatformConfig(
            enabled=True,
            webhook_url="https://example.test/feishu",
            pre_market_enabled=True,
            pre_market_time="08:50",
        ),
    )
    sent: list[tuple[str, str]] = []

    monkeypatch.setattr("src.notify.scheduler.build_summary", lambda: ("title", "body"))
    monkeypatch.setattr(
        "src.notify.scheduler.send",
        lambda platform, platform_cfg, title, markdown: sent.append((platform, title)) or (True, "ok"),
    )

    now = datetime(2026, 6, 24, 8, 50, tzinfo=CST)
    first = send_due_notifications(cfg, now)
    second = send_due_notifications(cfg, now)

    assert len(first) == 1
    assert second == []
    assert sent == [("feishu", "title [pre_market]")]
