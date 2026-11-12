"""The printable recovery kit of the household (EF-USR-023, ADR-0049, passes 10 K7 and 12 M8).

One self-contained HTML page (no script, no image, no link, no outside request) that says where
things are and how to get back in, and holds NO secret: not a password, not the passphrase of the
export, not a key of the keyring, not a token. It names the file of the keyring export and says
that the file lives apart from the passphrase. Nothing a secret could travel in is a parameter.
"""

import re
from html import escape

from shelfhold.i18n import t

KEYRING_EXPORT_NAME = "keyring.age"  # the `--out` of the documented `secrets export`
_MAX_NAME = 64
_NAME = re.compile(r"[^A-Za-z0-9._-]")

STYLE = (
    "body{font-family:sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#111}"
    "h1{font-size:1.5rem}h2{font-size:1.1rem;margin-top:1.6rem}"
    "pre{background:#f3f3f3;padding:.6rem;white-space:pre-wrap;border:1px solid #bbb}"
    ".fill{border-bottom:1px solid #111;height:1.6rem;margin:.4rem 0}"
    "@media print{body{margin:0}pre{background:none}}"
)


def _file_name(value: str) -> str:
    cleaned = _NAME.sub("", value)[:_MAX_NAME]
    return cleaned or KEYRING_EXPORT_NAME


def render_recovery_kit(*, keyring_export_name: str = KEYRING_EXPORT_NAME) -> str:
    """The page; the name of the export file is its only free text (cleaned and bounded)."""
    export_name = _file_name(keyring_export_name)
    parts = [
        "<!doctype html>",
        f'<html lang="fr"><head><meta charset="utf-8"><title>{escape(t("recovery_kit.title"))}'
        f"</title><style>{STYLE}</style></head><body>",
        f"<h1>{escape(t('recovery_kit.title'))}</h1>",
        f"<p>{escape(t('recovery_kit.intro'))}</p>",
    ]
    section = [
        ("recovery_kit.passphrase_title", "recovery_kit.passphrase_body", None, True),
        (
            "recovery_kit.reset_title",
            "recovery_kit.reset_body",
            "shelfhold admin reset-password --username NOM --password-file -",
            False,
        ),
        (
            "recovery_kit.sessions_title",
            "recovery_kit.sessions_body",
            "shelfhold admin sessions revoke --all",
            False,
        ),
        ("recovery_kit.kavita_title", "recovery_kit.kavita_body", None, False),
        (
            "recovery_kit.follows_title",
            "recovery_kit.follows_body",
            "shelfhold maintenance on",
            False,
        ),
        (
            "recovery_kit.keyring_title",
            "recovery_kit.keyring_body",
            f"shelfhold secrets export --passphrase-file PHRASE.txt --out {export_name}",
            False,
        ),
    ]
    for title, body, command, blank in section:
        parts.append(f"<h2>{escape(t(title))}</h2>")
        parts.append(f"<p>{escape(t(body, export=export_name))}</p>")
        if command:
            parts.append(f"<pre>{escape(command)}</pre>")
        if blank:
            parts.append('<div class="fill"></div><div class="fill"></div>')
    parts.append(f"<p>{escape(t('recovery_kit.rotation'))}</p>")
    parts.append(f"<p><em>{escape(t('recovery_kit.no_secret'))}</em></p></body></html>")
    return "".join(parts)
