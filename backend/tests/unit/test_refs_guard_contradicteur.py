"""#299, relecture adverse, question 6 : l'échec isolé de `test_secret_free_refs` (M2 L04).

Hypothesis le reproduit (`test_property_no_write_carries_a_registered_secret`) avec
`secret='0000000:'`, encodage `double`, gabarit `{}`, bruit `%` : la référence
`%0000000%253A`. Le `%` du bruit et les deux premiers chiffres forment un échappement `%00` qui
avale le début du secret. `rewrite_ref` et `RefsGuard.screen` la laissent passer telle quelle,
`check_write` la refuse. Pas de fuite (la garde d'écriture tient), mais les deux gardes ne
disent pas la même chose : la référence est acceptée à la frontière du plugin puis l'écriture en
base échoue. Le test de propriété n'est donc pas fragile : il trouve un vrai désaccord, rare
(150 exemples par passage).
"""

import pytest

from shelfhold.infrastructure.fetcher.refs import SECRET_PLACEHOLDER
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.write_guard import SecretInWriteError, check_write
from shelfhold.plugin_host.errors import RefHasSecret
from shelfhold.plugin_host.refs_guard import RefsGuard
from shelfhold.sdk.refs import SeriesRef
from tests.redaction_support import forget_secret_values

SECRET = "0000000" + ":"  # not a credential: the shrunk example of Hypothesis
REF = "%" + "0000000%253A"


@pytest.fixture(autouse=True)
def _clean_registry():  # type: ignore[no-untyped-def]
    forget_secret_values()
    yield
    forget_secret_values()


@pytest.mark.req("EF-EXP-065")
def test_the_write_guard_refuses_this_reference() -> None:
    register_secret_value(SECRET)
    with pytest.raises(SecretInWriteError):
        check_write("UPDATE unit_variant SET remote_unit_id = ?", (REF,), many=False)


@pytest.mark.req("EF-EXP-065")
@pytest.mark.parametrize("template", [None, SECRET_PLACEHOLDER])
def test_what_the_write_guard_refuses_the_refs_guard_refuses_or_hides(template: str | None) -> None:
    register_secret_value(SECRET)
    guard = (
        RefsGuard(lambda: (SECRET,), template=template)
        if template
        else RefsGuard(lambda: (SECRET,))
    )
    try:
        screened = guard.screen(
            SeriesRef(source_key="synthetic", remote_id=REF, title="t")
        ).remote_id
    except RefHasSecret:
        return
    check_write("UPDATE unit_variant SET remote_unit_id = ?", (screened,), many=False)
