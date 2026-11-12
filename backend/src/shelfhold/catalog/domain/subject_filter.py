"""The subject and word filter of the family search (EF-USR-018, M3 L19).

A remote result of a family source has no rating. Before a child may see even its title, this
filter drops what carries a flagged subject or word, in the title or in the subjects the source
gives (`memo["subjects"]`: the SDK key a source fills with the categories or subjects it knows).
A source that gives none is filtered on its title only: an absent subject is kept, an unreadable
one (not text, not a list of text) is dropped.

The default list always holds; a profile can add terms, never remove one. Before matching, the
text is folded so that what a child *reads* is what the filter *sees*: NFKC, format characters
(soft hyphen, zero width, joiners, bidi controls: category Cf) removed, case and accents folded,
look-alike letters (Cyrillic, Greek, small capitals) brought to Latin, digits used for letters
(`p0rn`) read as letters, and runs of single letters (`S.E.X.`) joined. Words of any script are
kept (the folding is on Unicode letters, never `[a-z]`).

Matching is by word: the roots of the default list match the start of a word (`sex` is `sexy`, not
`Essex`; a short list of exceptions covers `sexton`, `sextant`), its plain words match whole.
"""

# ruff: noqa: RUF001
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Final

__all__ = [
    "DEFAULT_ROOTS",
    "DEFAULT_WORDS",
    "MAX_TERMS",
    "MAX_TERM_CHARS",
    "InvalidTermError",
    "check_terms",
    "clean_terms",
    "is_flagged",
    "visible_title",
]

MAX_TERMS: Final = 100
MAX_TERM_CHARS: Final = 64
MAX_SUBJECTS: Final = 200
MAX_TEXT_CHARS: Final = 2000  # a longer text is read up to here: a bound on the work done

# Roots: they match the start of a word. English, French, German, Spanish, Italian, and the genre
# labels the sources use.
DEFAULT_ROOTS: Final = (
    "sex", "porn", "erot", "fetish", "fetich", "hentai", "bdsm", "nsfw", "smut", "ecchi", "yaoi",
    "nudity", "nudist", "nude", "nudita", "nudite", "nackt", "nacktheit", "desnud", "sesso",
    "sessual", "pornograf", "bondage", "incest", "prostitut", "stripper", "striptease", "orgy",
    "orgie", "nympho", "futanari", "cumshot", "kinky", "lewd", "explicit", "libertin", "doujin",
    "hardcore", "milf", "voyeur", "swinger",
)  # fmt: skip
# Plain words: they match whole.
DEFAULT_WORDS: Final = (
    "xxx", "lust", "ero", "rape", "raped", "rapist", "rapes", "kink", "r18", "adults only",
)  # fmt: skip
# Words that start with a root and are not what it stands for.
_EXCEPTIONS: Final = ("sext", "sexag", "sexen", "sexcen", "nuder", "nudg", "ecchin", "erotyl")
_NOT_EXCEPTIONS: Final = ("sexting",)

_REVERSING = (
    "\u202b",
    "\u202e",
    "\u2067",
)  # bidi embeddings and override: the text reads backwards
_BLANKS = str.maketrans(dict.fromkeys((0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800, 0x180E), " "))
_ENCLOSED = str.maketrans(  # circled and squared letters (NFKC leaves the negative ones)
    {
        base + i: chr(ord("a") + i)
        for base in (0x1F130, 0x1F150, 0x1F170, 0x1F1E6, 0x24B6, 0x24D0)
        for i in range(26)
    }
)
_SIGN = re.compile(r"(?<=[^\W\d_])[$@]|[$@](?=[^\W\d_])")  # a sign beside a letter is a letter
_NOT_WORD = re.compile(r"[\W_]+")
_UNSPACED = re.compile(r"[฀-๿぀-ヿ㐀-鿿가-힯]")
_LEET = str.maketrans("013457", "oieast")
_ALIKE = str.maketrans(
    {
        # Cyrillic
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x",
        "у": "y", "і": "i", "ѕ": "s", "ј": "j", "к": "k", "м": "m",
        "н": "h", "т": "t", "в": "b", "ԁ": "d", "һ": "h",
        # Greek
        "α": "a", "ε": "e", "ο": "o", "ρ": "p", "ι": "i", "κ": "k",
        "ν": "v", "τ": "t", "υ": "u", "χ": "x", "β": "b", "η": "n",
        "ω": "w", "σ": "s", "ς": "s", "ζ": "z",
        # small capitals and relatives
        "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f",
        "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l",
        "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ʀ": "r", "ꜱ": "s",
        "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
        # Armenian and Cherokee (upper case: `casefold` maps the lower case of Cherokee up)
        "\u0585": "o", "\u057d": "u", "\u0578": "n", "\u0581": "g", "\u0570": "h",
        "\u0575": "j", "\u051b": "q", "\u051d": "w",
        "\u13aa": "a", "\u13f4": "b", "\u13df": "c", "\u13a0": "d", "\u13ac": "e",
        "\u13c0": "g", "\u13bb": "h", "\u13b5": "i", "\u13ab": "j", "\u13e6": "k",
        "\u13de": "l", "\u13b7": "m", "\u13c1": "n", "\u13e2": "p", "\u13a1": "r",
        "\u13da": "s", "\u13a2": "t", "\u13e9": "v", "\u13b3": "w", "\u13d4": "x",
        "\u13a9": "y", "\u13c3": "z",
        "ı": "i", "ł": "l", "ø": "o", "đ": "d",
    }
)  # fmt: skip


class InvalidTermError(ValueError):
    """A word the profile gave that folds to nothing: it would flag nothing, or everything."""


def _plain(text: str, at: str = "a") -> str:
    """NFKC, without format characters, lowercase, accents of Latin, Greek and Cyrillic letters
    folded (a mark of another script is part of its letter and stays), then words apart."""
    text = unicodedata.normalize("NFKC", text[:MAX_TEXT_CHARS])
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    text = text.translate(_BLANKS).translate(_ENCLOSED)
    text = _SIGN.sub(lambda m: at if m.group() == "@" else "s", text)
    out: list[str] = []
    for c in unicodedata.normalize("NFKD", text.casefold()):
        if unicodedata.category(c) == "Mn" and out and (out[-1].isascii() or "Ͱ" <= out[-1] <= "ԯ"):
            continue
        out.append(c)
    return _NOT_WORD.sub(" ", "".join(out)).strip()


def _tokens(plain: str) -> list[str]:
    """The words of a plain text as the filter reads them: letters that look alike brought to
    Latin, digits read as letters in a word that has letters, runs of single letters joined (and
    kept beside)."""
    words: list[str] = []
    for word in plain.translate(_ALIKE).split():
        if any(c.isalpha() for c in word):
            word = word.translate(_LEET)
        words.append(word)
    joined: list[str] = []
    run: list[str] = []
    for word in [*words, ""]:
        if len(word) == 1 and word.isalpha():
            run.append(word)
            continue
        if len(run) >= 3:
            joined.append("".join(run))
        run = []
    return words + joined


def _root_match(word: str, root: str) -> bool:
    if not word.startswith(root):
        return False
    if word.startswith(_NOT_EXCEPTIONS):
        return True
    return not word.startswith(_EXCEPTIONS)


def _split_root(words: list[str]) -> bool:
    """A root cut by separators (`Por-no`, `se x`): a word that is the start of a root, put
    together with the words after it, begins with that root. Only from such a start, so `Essex`
    or `Sussex` (which do not begin with the root) and `Sextant` (an exception) stay clear."""
    for i, word in enumerate(words):
        for root in DEFAULT_ROOTS:
            if len(word) >= len(root) or not root.startswith(word):
                continue
            glued = word
            for following in words[i + 1 : i + 5]:
                glued += following
                if len(glued) >= len(root):
                    break
            if _root_match(glued, root):
                return True
    return False


def clean_terms(raw: object) -> tuple[str, ...]:
    """The terms of a profile as they are stored: text only, in plain form, unique, bounded.
    Tolerant (a stored list is read as it is); `check_terms` is the strict door of a write."""
    if not isinstance(raw, list | tuple):
        return ()
    found: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        term = _plain(item)[:MAX_TERM_CHARS].strip()
        if term and term not in found:
            found.append(term)
        if len(found) >= MAX_TERMS:
            break
    return tuple(found)


def check_terms(raw: Iterable[str]) -> tuple[str, ...]:
    """The terms of a write: each must keep at least one letter or digit after folding, or the
    whole write is refused (`InvalidTermError`): a word is never dropped in silence."""
    items = list(raw)
    kept = clean_terms(items)
    for item in items:
        if not isinstance(item, str) or not _plain(item):
            raise InvalidTermError("invalid_term")
    return kept


def visible_title(title: str) -> str:
    """A title as it is shown to a child: without the characters that are not seen (format
    characters, controls) that a filter could be dodged with."""
    shown = "".join(c for c in title if unicodedata.category(c) not in ("Cf", "Cc"))
    return shown.strip()


def _subjects(memo: Mapping[str, object]) -> list[str] | None:
    """The subjects a source gave, or None when what it gave cannot be read."""
    if "subjects" not in memo:
        return []
    raw = memo["subjects"]
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list | tuple) and len(raw) <= MAX_SUBJECTS:
        texts = [s for s in raw if isinstance(s, str)]
        if len(texts) == len(raw):
            return texts
    return None


def _holds(plain: str, words: list[str], term: str) -> bool:
    wanted = _tokens(term)
    if not wanted:
        return False
    if len(wanted) == 1:
        if _UNSPACED.search(term):
            return term in plain
        return wanted[0] in words
    spaced = f" {' '.join(wanted)} "
    return spaced in f" {' '.join(words)} "


def is_flagged(title: str, memo: Mapping[str, object], terms: Iterable[str] = ()) -> bool:
    """Whether this remote result is dropped: a flagged word in the title or in a subject."""
    subjects = _subjects(memo)
    if subjects is None:
        return True
    own = clean_terms(list(terms))
    for text in (title, *subjects):
        reads = [_plain(text)]
        if "@" in text:  # `@` is an a or an o: read both ways
            reads.append(_plain(text, "o"))
        if any(c in text for c in _REVERSING):
            reads += [_plain(text[::-1], "a"), _plain(text[::-1], "o")]
        for seen in reads:  # a text made to read backwards is read both ways
            words = _tokens(seen)
            if any(_root_match(w, r) for w in words for r in DEFAULT_ROOTS):
                return True
            if _split_root(words):
                return True
            if any(_holds(seen, words, term) for term in (*DEFAULT_WORDS, *own)):
                return True
    return False
