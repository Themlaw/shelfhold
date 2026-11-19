"""M5 L04: what adds a way in refuses a provider's proof alone, by effect (EF-USR-024).

The rule hangs on the effects of the catalogue, not on a list of actions. These tests walk the
whole catalogue, so an action that carries one of the effects is covered without being named, and
an effect that creates an access has to be put in the set on purpose.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shelfhold.identity.domain.catalogue import RESERVED_ACTIONS
from shelfhold.identity.domain.credentials import Credential, Principal
from shelfhold.identity.domain.reserved import (
    DURABLE_ACCESS_EFFECTS,
    Allowed,
    Effect,
    ReauthRequired,
    authorize_reserved,
)

pytestmark = pytest.mark.req("EF-USR-024")

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
ADMIN = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.SESSION)
BY_PROVIDER = replace(ADMIN, auth_time=NOW, provider_proof=True)
BY_PASSWORD = replace(ADMIN, auth_time=NOW, provider_proof=False)


def verdict(principal: Principal, name: str, *, local_factor: bool) -> object:
    return authorize_reserved(
        principal,
        RESERVED_ACTIONS[name],
        now=NOW,
        fresh_auth_s=900,
        restricted_profile_exists=False,
        hardened=False,
        has_local_factor=local_factor,
    )


DURABLE_ACTIONS = sorted(
    name for name, action in RESERVED_ACTIONS.items() if action.effects & DURABLE_ACCESS_EFFECTS
)


def test_the_effects_that_create_an_access_are_the_ones_decided() -> None:
    """Adding or removing one is a security decision: it shows here, in review."""
    assert {
        Effect.ENROLS_PASSKEY,
        Effect.LINKS_IDENTITY,
        Effect.MANAGES_CREDENTIALS,
        Effect.TOUCHES_SECRETS,
        Effect.MANAGES_PROFILES,
    } == DURABLE_ACCESS_EFFECTS


def test_the_decided_actions_are_in_the_catalogue() -> None:
    """The pairing, the rotation of a token and the profiles are what the second review found."""
    assert {
        "device_token.issue",
        "device_token.rotate",
        "profile.create",
        "profile.update",
        "passkey.register",
        "oidc.link_own",
    } <= set(DURABLE_ACTIONS)


@pytest.mark.parametrize("name", DURABLE_ACTIONS)
def test_every_durable_action_refuses_a_provider_proof(name: str) -> None:
    refused = verdict(BY_PROVIDER, name, local_factor=True)
    assert isinstance(refused, ReauthRequired) and refused.local_only, name
    # an account with no local factor has no other authority than the provider's
    assert isinstance(verdict(BY_PROVIDER, name, local_factor=False), Allowed)
    # a proof given here is the one the rule asks for
    assert isinstance(verdict(BY_PASSWORD, name, local_factor=True), Allowed)


@pytest.mark.parametrize("name", sorted(set(RESERVED_ACTIONS) - set(DURABLE_ACTIONS)))
def test_an_action_that_adds_no_access_keeps_the_provider_proof(name: str) -> None:
    """Unlinking and the like: the provider's proof stays enough, the rule is not wider than it
    says."""
    answer = verdict(BY_PROVIDER, name, local_factor=True)
    assert not (isinstance(answer, ReauthRequired) and answer.local_only), name
