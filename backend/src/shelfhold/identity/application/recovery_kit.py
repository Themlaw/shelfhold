"""The printable recovery kit of the household (EF-USR-023, ADR-0049, passes 10 K7 and 12 M8).

One HTML page (no script, no image, no outside request: its only link is the style sheet of this
server) that says where things are and how to get back in, and holds NO secret: not a password,
not the passphrase of the export, not a key of the keyring, not a token. It names the file of
the keyring export and says that the file lives apart from the passphrase.
Nothing a secret could travel in is a parameter.
"""

import re
from html import escape

from shelfhold.i18n import t_lang

KEYRING_EXPORT_NAME = "keyring.age"  # the `--out` of the documented `secrets export`
_MAX_NAME = 64
_NAME = re.compile(r"[^A-Za-z0-9._-]")

# The sheet of the server's own pages (`interfaces/http_api/page_assets.STYLE_PATH`, which a test
# keeps equal to this): the page links it, and holds no style of its own, so that its policy
# (`default-src 'none'`, styles of this origin only) forbids anything inline.
STYLE_HREF = "/pages/paper-ink.css"


def _file_name(value: str) -> str:
    cleaned = _NAME.sub("", value)[:_MAX_NAME]
    return cleaned or KEYRING_EXPORT_NAME


def render_recovery_kit(
    *, keyring_export_name: str = KEYRING_EXPORT_NAME, language: str = "fr"
) -> str:
    """The page, in the language of whoever opens it; the name of the export file is its only free
    text (cleaned and bounded)."""

    def t(key: str, **params: object) -> str:
        return escape(t_lang(key, language=language, **params))

    export = _file_name(keyring_export_name)
    parts = [
        "<!doctype html>",
        f'<html lang="{escape(language)}"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="robots" content="noindex, nofollow">',
        '<meta name="color-scheme" content="light dark">',
        f"<title>{t('recovery_kit.title')}</title>",
        f'<link rel="stylesheet" href="{STYLE_HREF}"></head><body><main class="kit">',
        f"<h1>{t('recovery_kit.title')}</h1>",
        f'<p class="kit__intro">{t("recovery_kit.intro")}</p>',
    ]
    section = [
        (t("recovery_kit.passphrase_title"), t("recovery_kit.passphrase_body"), None, True),
        (
            t("recovery_kit.reset_title"),
            t("recovery_kit.reset_body"),
            "shelfhold admin reset-password --username NOM --password-file -",
            False,
        ),
        (
            t("recovery_kit.sessions_title"),
            t("recovery_kit.sessions_body"),
            "shelfhold admin sessions revoke --all",
            False,
        ),
        (t("recovery_kit.kavita_title"), t("recovery_kit.kavita_body"), None, False),
        (
            t("recovery_kit.follows_title"),
            t("recovery_kit.follows_body"),
            "shelfhold maintenance on",
            False,
        ),
        (
            t("recovery_kit.keyring_title"),
            t("recovery_kit.keyring_body", export=export),
            f"shelfhold secrets export --passphrase-file PHRASE.txt --out {export}",
            True,
        ),
    ]
    for title, body, command, blank in section:
        parts.append(f"<section><h2>{title}</h2><p>{body}</p>")
        if command:
            parts.append(f'<pre class="cmd">{escape(command)}</pre>')
        if blank:
            parts.append(
                '<p class="fill" aria-hidden="true"></p><p class="fill" aria-hidden="true"></p>'
            )
        parts.append("</section>")
    parts.append(
        f'<div class="kit__close"><p>{t("recovery_kit.rotation")}</p>'
        f"<p><em>{t('recovery_kit.no_secret')}</em></p></div></main></body></html>"
    )
    return "".join(parts)
