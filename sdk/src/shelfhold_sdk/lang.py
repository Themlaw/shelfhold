# SPDX-License-Identifier: MIT
"""Language type of the SDK (ENF-I18N-005).

`LanguageTag` is a canonical BCP 47 tag validated by the host (`normalize_lang`, catalog side): a
plugin never normalizes, it passes on the value received from upstream and the host converts it at
ingestion. The SDK therefore only carries the type and the list of values that are not languages.
"""

from typing import NewType

LanguageTag = NewType("LanguageTag", str)

# Placeholder values, never an Edition language. A multi-language source (`all`, `multi`) or an
# unknown one (`und`, `mul`, empty) yields to the chapter's language, otherwise the Unit waits.
PLACEHOLDER_LANGUAGES: frozenset[str] = frozenset({"all", "multi", "und", "mul", ""})
