# SPDX-License-Identifier: MIT
"""Type de langue du SDK (ENF-I18N-005).

`LanguageTag` est une étiquette BCP 47 canonique, validée **par l'hôte** (`normalize_lang`, côté
catalogue) : un plugin ne normalise jamais, il transmet la valeur reçue de l'amont et l'hôte la
convertit à l'ingestion. Le SDK ne porte donc que le type et la liste des valeurs qui ne sont pas
des langues.
"""

from typing import NewType

LanguageTag = NewType("LanguageTag", str)

# Valeurs « jokers » : jamais une langue d'Édition. Une source multilingue (`all`, `multi`) ou
# inconnue (`und`, `mul`, vide) cède la place à la langue du chapitre, sinon l'Unité attend.
PLACEHOLDER_LANGUAGES: frozenset[str] = frozenset({"all", "multi", "und", "mul", ""})
