"""ENF-SEC-015, EF-INT-006: upstream text in a notification is neutralised for Markdown.

`sanitize_upstream_text` escapes Markdown syntax; it cannot stop the recipient (Discord, Slack,
Matrix, Telegram) from auto-linking a bare URL or resolving `@everyone`, so the notification
layer marks both explicitly (PLAN-M1a section 7, review of PR #40).
"""

from __future__ import annotations

import re

import pytest

from shelfhold.integrations.apprise import render_notification

HOSTILE = [
    "visit https://evil.example/login?next=1 now",
    "www.evil.example/phish",
    "go to evil.example/path",
    "ping @everyone and @here please",
    "<@123456789> <@&987654321> <#555> <!channel> <!here> <!subteam^S123>",
    "[click](https://evil.example) ![img](https://evil.example/x.png)",
    "**bold** _it_ `code` ~~s~~ # heading\n- item\n1. item",
    "<script>alert(1)</script><img src=x onerror=alert(1)>",
    "text&#32;https://evil.example/&amp;x",
    "https://evil.example/a_b_c_(d)",
    "x@everyone hello@everyone _@everyone_ \\`@everyone",
    "`https://`evil.example/x`",
]
CODE_SPAN = re.compile(r"`[^`\n]*`")


def outside_code(text: str) -> str:
    return CODE_SPAN.sub("", re.sub(r"\\.", "", text))  # escaped characters are literal


@pytest.mark.req("ENF-SEC-015", "EF-INT-006")
@pytest.mark.parametrize("hostile", HOSTILE)
def test_notifications_escape_upstream_text(hostile: str) -> None:
    message = render_notification("source.broken", {"count": 2}, upstream={"series": hostile})
    visible = outside_code(message.body)
    assert not re.search(r"(?i)\b(?:https?|ftp)://", visible)  # no bare URL left to autolink
    assert not re.search(r"(?i)\bwww\.", visible)
    assert not re.search(r"(?i)\b[a-z0-9-]+\.(?:example|com|org)/", visible)
    assert "@everyone" not in visible
    assert "@here" not in visible
    assert not re.search(r"<[@!#&]", visible)  # Discord and Slack mention syntax
    assert "<script" not in message.body.lower().replace("\\<", "<")  # escaped, if kept at all
    assert not re.search(r"(?<!\\)<", visible)
    assert not re.search(r"(?<!\\)\[[^\]]*\]\(", visible)  # no live Markdown link
    assert message.title.startswith("Shelfhold")


@pytest.mark.req("ENF-SEC-015", "EF-INT-006")
def test_urls_and_mentions_are_marked_explicitly_not_hidden() -> None:
    message = render_notification(
        "source.broken", {}, upstream={"series": "see https://evil.example/x @everyone"}
    )
    assert "`https://evil.example/x`" in message.body  # a code span: shown, never a link
    assert "`@everyone`" in message.body
    assert "​" not in message.body  # no zero-width trick (sanitize strips them anyway)


@pytest.mark.req("ENF-SEC-015", "EF-INT-006")
def test_plain_text_and_our_own_codes_survive() -> None:
    message = render_notification("disk.low", {"count": 3})
    assert "disk.low" in message.body
    assert "3" in message.body
    plain = render_notification("series.gap", {}, upstream={"series": "Re:Zero [Light Novel] 2"})
    assert "Re:Zero" in plain.body
    assert not re.search(r"(?<!\\)\[Light", plain.body)


@pytest.mark.req("ENF-SEC-015")
def test_upstream_text_is_bounded() -> None:
    message = render_notification("series.gap", {}, upstream={"series": "x" * 100_000})
    assert len(message.body) < 2_000
