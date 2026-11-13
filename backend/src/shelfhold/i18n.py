"""Message catalogs (ENF-I18N-001) and French typography (ENF-I18N-003).

French since M0, English since L51; every user-facing string is a catalog message. A message is
an ICU subset:

- `{count, plural, =0 {no file} one {# file} other {# files}}`: exact `=N` first, then the CLDR
  category of the language (`one` is 0 and 1 in French, only 1 in English; `many` is a whole
  number of millions, else `other`; a missing `many` falls back to `other`). `#` is the number,
  grouped as `Intl` does for the language. A branch may hold `{name}` placeholders and other
  plurals.
- `{size:fr}` or `{size:en}`: a number grouped as `Intl` writes it for that language, not a bare
  digit run.
- every other `{name}` is a `str.format` placeholder, `{{` and `}}` the literal braces.

The typography is applied once, to the catalog text, when it is loaded: a plain space before `;`,
`:`, `!` or `?` becomes a narrow no-break space (U+202F), except in a code span between
backticks. (English text carries none, so it passes through unchanged.) A value (a title, a
path, a name sent by a source) is never retypeset.
"""

import re
import tomllib
from functools import cache
from importlib import resources
from typing import Any

DEFAULT_LANGUAGE = "fr"
LANGUAGES = ("fr", "en")
NARROW_NBSP = chr(0x202F)

_BEFORE_PUNCTUATION = re.compile(f"[ {chr(0x00A0)}{NARROW_NBSP}]+(?=[;:!?])")
_CODE_SPAN = re.compile(r"(`[^`]*`)")
_GROUPED = re.compile(r"\{(\w+):(fr|en)\}")
_PLURAL_HEAD = re.compile(r"\s*(\w+)\s*,\s*plural\s*,")
_SELECTOR = re.compile(r"\s*(=?\w+)\s*\{")


def typeset(text: str) -> str:
    """Narrow no-break space before `;:!?`, outside code spans."""
    parts = _CODE_SPAN.split(text)
    return "".join(
        part if index % 2 else _BEFORE_PUNCTUATION.sub(NARROW_NBSP, part)
        for index, part in enumerate(parts)
    )


def _flatten(table: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in table.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, dotted + "."))
        else:
            flat[dotted] = str(value)
    return flat


@cache
def load_catalog(language: str = DEFAULT_LANGUAGE) -> dict[str, str]:
    text = resources.files("shelfhold").joinpath("locales", f"{language}.toml").read_text("utf-8")
    return {key: typeset(value) for key, value in _flatten(tomllib.loads(text)).items()}


def _grouped(value: object, language: str) -> str:
    """`1234567.5` is `1 234 567,5` with narrow no-break spaces in French, `1,234,567.5` in
    English: whatever `Intl` writes for the language of the message."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"not a number: {value!r}")
    text = f"{value:,}"
    if language != DEFAULT_LANGUAGE:
        return text  # English already writes it this way
    return text.replace(",", NARROW_NBSP).replace(".", ",")


def _category(value: int | float, language: str) -> str:
    if language != DEFAULT_LANGUAGE:  # English: only `one` is singular, and only 1
        return "one" if value == 1 else "other"
    if abs(value) < 2:  # 0, 1 and 1.5 are singular in French
        return "one"
    if value == int(value) and int(value) % 1_000_000 == 0:
        return "many"
    return "other"


def _closing(text: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError(f"unbalanced braces in {text!r}")


def _choose(inner: str, params: dict[str, object], language: str) -> str | None:
    head = _PLURAL_HEAD.match(inner)
    if head is None:
        return None
    value = params[head.group(1)]
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)  # a count already written as digits
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"plural argument {head.group(1)!r} is not a number: {value!r}")
    branches: dict[str, str] = {}
    position = head.end()
    while (selector := _SELECTOR.match(inner, position)) is not None:
        opening = selector.end() - 1
        closing = _closing(inner, opening)
        branches[selector.group(1)] = inner[opening + 1 : closing]
        position = closing + 1
    if inner[position:].strip():
        raise ValueError(f"unreadable plural in {inner!r}")
    category = _category(value, language)
    chosen = branches.get(f"={int(value) if value == int(value) else value}")
    if chosen is None:
        chosen = branches.get(category) or branches.get("other")
    if chosen is None:
        raise ValueError(f"plural without `other` in {inner!r}")
    return _expand(chosen, params, language).replace("#", _grouped(value, language))


def _expand(template: str, params: dict[str, object], language: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(template):
        if template.startswith(("{{", "}}"), index):
            out.append(template[index : index + 2])
            index += 2
        elif template[index] == "{":
            closing = _closing(template, index)
            chosen = _choose(template[index + 1 : closing], params, language)
            out.append(template[index : closing + 1] if chosen is None else chosen)
            index = closing + 1
        else:
            out.append(template[index])
            index += 1
    return "".join(out)


def render(template: str, *, language: str = DEFAULT_LANGUAGE, **params: object) -> str:
    """Fill a catalog message: plurals, grouped numbers, then the `str.format` placeholders."""
    expanded = _expand(template, params, language)
    expanded = _GROUPED.sub(
        lambda match: _grouped(params[match.group(1)], match.group(2)), expanded
    )
    return expanded.format(**params)


def t(key: str, **params: object) -> str:
    """Return the localized message for `key` in French, the language of the CLI; an unknown key
    raises KeyError. An HTTP answer picks its language with `t_lang` instead."""
    return t_lang(key, language=DEFAULT_LANGUAGE, **params)


def t_lang(key: str, *, language: str = DEFAULT_LANGUAGE, **params: object) -> str:
    """Return the localized message for `key`, in the language of the request or of the profile."""
    return render(load_catalog(language)[key], language=language, **params)


def negotiate(accept_language: str | None) -> str:
    """The language of an answer when no profile preference says otherwise (HTTP `Accept-Language`).

    The first preferred language the catalog speaks wins; a language with `q=0` is excluded, and
    anything else (no header, `*`, a language that is not delivered) falls back to the default."""
    candidates: list[tuple[float, int, str]] = []
    for position, part in enumerate((accept_language or "").split(",")):
        pieces = part.strip().split(";")
        tag = pieces[0].strip().lower()
        if not tag or tag == "*":
            continue
        quality = 1.0
        for piece in pieces[1:]:
            if piece.strip().startswith("q="):
                try:
                    quality = float(piece.strip()[2:])
                except ValueError:
                    quality = 0.0
        if quality > 0:
            candidates.append((-quality, position, tag.split("-")[0]))
    for _quality, _position, tag in sorted(candidates):
        if tag in LANGUAGES:
            return tag
    return DEFAULT_LANGUAGE
