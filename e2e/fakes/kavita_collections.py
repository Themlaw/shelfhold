"""The window of the E2E on the collections and reading lists of the simulated Kavita (EF-EXP-025).

The simulated server (`tests/fakes/kavita/collections.py`, behind a socket in `kavita/serve.py`)
keeps them under the account of the key that made them and shows a promoted one to every account,
each cut down to the series of its libraries. `GET /_fake/collections?name=<account>` tells what
that account is shown: `{"collections": {title: [series names]}, "lists": {title: [series names]}}`.
Never used by Shelfhold; the test's own view of "what the child's account sees in Kavita".
"""

from __future__ import annotations

from typing import Any


def seen_by(fake: Any, account: str) -> dict[str, dict[str, list[str]]]:
    """What `account` is shown of the collections and lists, by series name."""
    names = {s["id"]: s["name"] for s in fake.series}
    shown = fake.shelves.seen_by(fake, account)
    return {
        kind: {title: [names[i] for i in ids if i in names] for title, ids in held.items()}
        for kind, held in shown.items()
    }
