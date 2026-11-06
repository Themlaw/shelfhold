"""`docs/reference/config.md`, generated from the real definition of the settings (EF-INT-024).

Two sources, both read from the running code and never copied by hand: `Settings` (the
environment variables and `config.toml`) and `headless.keys.KEYS` (the keys of `config get|set`
and of the declarative file of `config apply`). A test fails when the committed file differs from
what this module renders.

    python -m shelfhold.docgen.config_reference > docs/reference/config.md
"""

import sys
from pathlib import Path
from typing import Any, get_args, get_origin

from pydantic import SecretStr
from pydantic.fields import FieldInfo

from shelfhold.config import CONFIG_SCHEMA_VERSION, ENV_ONLY_FIELDS, ENV_PREFIX, Settings
from shelfhold.headless.keys import KEYS, PATH_MAP_PREFIX

REFERENCE_PATH = Path("docs") / "reference" / "config.md"


def _type_name(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is None:
        return (
            "secret"
            if annotation is SecretStr
            else getattr(annotation, "__name__", str(annotation))
        )
    arguments = [a for a in get_args(annotation) if a is not type(None)]
    if len(arguments) != len(get_args(annotation)) and len(arguments) == 1:
        return _type_name(arguments[0]) + " (optionnel)"
    if origin is tuple:
        return "liste de " + _type_name(arguments[0])
    return " | ".join(repr(a) if isinstance(a, str) else _type_name(a) for a in arguments)


def _bounds(field: FieldInfo) -> str:
    parts = []
    for item in field.metadata:
        for attribute, sign in (("ge", ">="), ("gt", ">"), ("le", "<="), ("lt", "<")):
            value = getattr(item, attribute, None)
            if value is not None:
                parts.append(f"{sign} {value}")
    return ", ".join(parts)


def _shown(value: object, name: str = "") -> str:
    if name == "umask" and isinstance(value, int):
        return f"`{value:04o}`"
    if value is None or value == () or value == []:
        return "aucun"
    if isinstance(value, bool):
        return "`true`" if value else "`false`"
    return f"`{value}`"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _settings_table() -> list[str]:
    lines = [
        "| Variable | Clé de `config.toml` | Type | Défaut | Bornes | Remarque |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, field in Settings.model_fields.items():
        notes = []
        if name in ENV_ONLY_FIELDS:
            notes.append("variable seule, jamais dans `config.toml`")
        if field.annotation is SecretStr or SecretStr in get_args(field.annotation):
            notes.append("secret, masqué partout")
        if not name.endswith("_file"):
            notes.append(f"`{ENV_PREFIX}{name.upper()}_FILE` en lit la valeur dans un fichier")
        lines.append(
            f"| `{ENV_PREFIX}{name.upper()}` | `{name}` | {_cell(_type_name(field.annotation))} | "
            f"{_shown(field.default, name)} | {_cell(_bounds(field))} | {'; '.join(notes) or '-'} |"
        )
    return lines


def _keys_table() -> list[str]:
    lines = [
        "| Clé | Défaut | Modifiable | Secret | Rôle |",
        "| --- | --- | --- | --- | --- |",
    ]
    for key in KEYS.values():
        default = key.default_doc or _shown(key.default)
        editable = "non (lecture seule)" if key.readonly else "oui"
        secret = "oui" if key.secret else "non"
        lines.append(f"| `{key.name}` | {default} | {editable} | {secret} | {_cell(key.help)} |")
    lines.append(
        f"| `{PATH_MAP_PREFIX}<cible>` | selon la cible | oui | non | "
        "Correspondance des chemins de la destination (JSON) ; une clé par cible. |"
    )
    return lines


def render() -> str:
    lines = [
        "# Référence de la configuration",
        "",
        "<!-- Fichier généré : `python -m shelfhold.docgen.config_reference`.",
        "     Ne pas modifier à la main : un test échoue si ce fichier n'est pas à jour. -->",
        "",
        f"Version du schéma de `config.toml` : `{CONFIG_SCHEMA_VERSION}`. Précédence : variable "
        "d'environnement, puis `config.toml`, puis base de données, puis valeur par défaut. "
        "Le texte d'accompagnement (rôle de chaque variable, exemples) est dans "
        "[`docs/configuration.md`](../configuration.md), les commandes dans "
        "[`docs/admin/headless.md`](../admin/headless.md).",
        "",
        "## Variables d'environnement et `config.toml`",
        "",
        *_settings_table(),
        "",
        "## Clés de `config get|set|apply`",
        "",
        "Enregistrées en base ; l'environnement et `config.toml` l'emportent. "
        "Le rôle est donné tel que le code le décrit.",
        "",
        *_keys_table(),
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
