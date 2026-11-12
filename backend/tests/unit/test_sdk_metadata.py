"""MetadataProvider contract typed by entity (EF-BIB-013, SDK 1.2) and its conformance kit."""

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from shelfhold.sdk.metadata import (
    EntityRef,
    FieldProposal,
    IdentifyResult,
    MatchHints,
    MetadataManifest,
    Provenance,
    RelationProposal,
)
from shelfhold.sdk.models import SDK_VERSION, PluginKind, sdk_supports
from shelfhold.sdk.testing.fakes import (
    FAKE_ISBN,
    FakeMetadataProvider,
    NetworkForbidden,
    fake_metadata_fetcher,
)
from shelfhold.sdk.testing.kit import ConformanceReport, run_metadata_conformance

EDITION = EntityRef(entity_type="edition", ids={"isbn13": FAKE_ISBN})
HINTS = MatchHints(title="Fake Book")


def _run(
    provider: FakeMetadataProvider, targets: list[EntityRef] | None = None
) -> ConformanceReport:
    return asyncio.run(
        run_metadata_conformance(
            provider, fake_metadata_fetcher(), targets=targets or [EDITION], hints=HINTS
        )
    )


def _check(report: ConformanceReport, name: str) -> bool:
    return next(c.ok for c in report.checks if c.name == name)


@pytest.mark.req("EF-BIB-013")
def test_metadata_provider_isbn_targets_edition_not_work() -> None:
    """Rayonnage form: an ISBN designates an Edition or a Volume, never a Work."""
    for kind in ("edition", "volume"):
        assert EntityRef(entity_type=kind, ids={"isbn13": FAKE_ISBN}).entity_type == kind
    for kind in ("work", "series"):
        with pytest.raises(ValidationError):
            EntityRef(entity_type=kind, ids={"isbn13": FAKE_ISBN})
    # a provider answering an ISBN target with Work-level fields fails the kit
    provider = FakeMetadataProvider(answer_entity_type="work")
    report = _run(provider)
    assert not report.ok
    assert not _check(report, "isbn_targets_edition")
    # the honest fake proposes on the Edition and carries the Work through a relation
    good = asyncio.run(FakeMetadataProvider().identify(fake_metadata_fetcher(), EDITION, HINTS))
    assert {p.entity_type for p in good.proposals} == {"edition"}
    assert _check(_run(FakeMetadataProvider()), "isbn_targets_edition")


@pytest.mark.req("EF-BIB-013")
def test_metadata_provider_roundtrip_json() -> None:
    fetcher = fake_metadata_fetcher()
    provider = FakeMetadataProvider()
    result = asyncio.run(provider.identify(fetcher, EDITION, HINTS))
    assert result.proposals and result.relations
    for model in (provider.manifest, EDITION, HINTS, result, *result.proposals, *result.relations):
        assert type(model).model_validate_json(model.model_dump_json()) == model
    proposal = FieldProposal(
        entity_type="edition",
        field="title",
        lang="fr",
        value="Un titre",
        confidence=0.8,
        provenance=Provenance(provider="fake-meta", fetched_at=datetime(2026, 1, 2, tzinfo=UTC)),
    )
    assert FieldProposal.model_validate_json(proposal.model_dump_json()) == proposal
    with pytest.raises(ValidationError):
        FieldProposal.model_validate({**proposal.model_dump(), "confidence": 1.5})
    assert isinstance(result, IdentifyResult)
    assert all(isinstance(r, RelationProposal) for r in result.relations)


@pytest.mark.req("EF-BIB-013", "ENF-QUAL-003")
def test_metadata_conformance_kit_passes_on_fake_provider_offline() -> None:
    fetcher = fake_metadata_fetcher()
    provider = FakeMetadataProvider()
    report = asyncio.run(
        run_metadata_conformance(provider, fetcher, targets=[EDITION], hints=HINTS)
    )
    assert report.ok, report.failures
    names = {c.name for c in report.checks}
    assert {"manifest", "api_compatible", "sdk_compatible", "terms_url", "quota",
            "capabilities_implemented", "proposals_typed", "isbn_targets_edition",
            "url_roundtrip", "json_roundtrip", "offline"} <= names  # fmt: skip
    assert fetcher.record_mode == "none"
    assert fetcher.unscripted == []


@pytest.mark.req("EF-BIB-013")
def test_kit_flags_field_outside_touched_fields_and_wrong_provenance() -> None:
    assert not _check(_run(FakeMetadataProvider(extra_field="secret_notes")), "proposals_typed")
    assert not _check(_run(FakeMetadataProvider(provenance_key="someone-else")), "proposals_typed")


@pytest.mark.req("EF-BIB-013", "ENF-LEG-003")
def test_kit_requires_terms_url_and_declared_quota() -> None:
    assert not _check(_run(FakeMetadataProvider(terms_url=None)), "terms_url")
    assert not _check(_run(FakeMetadataProvider(terms_url="http://plain.invalid/")), "terms_url")
    assert not _check(_run(FakeMetadataProvider(requests_per_second=0.0)), "quota")


@pytest.mark.req("EF-BIB-013")
def test_kit_flags_target_type_the_provider_does_not_declare() -> None:
    work = EntityRef(entity_type="work", ids={"openlibrary": "OL1W"})
    report = _run(FakeMetadataProvider(), [EDITION, work])
    assert not _check(report, "entity_types_declared")


@pytest.mark.req("EF-BIB-013")
def test_kit_flags_url_mapping_that_does_not_roundtrip() -> None:
    assert not _check(_run(FakeMetadataProvider(break_url_mapping=True)), "url_roundtrip")


@pytest.mark.req("EF-BIB-013", "ENF-QUAL-003")
def test_kit_flags_network_access_outside_the_script() -> None:
    provider = FakeMetadataProvider(leak_to="https://real-site.invalid/x")
    report = _run(provider)
    assert not report.ok and not _check(report, "offline")
    with pytest.raises(NetworkForbidden):
        asyncio.run(fake_metadata_fetcher().get("https://unscripted.invalid/"))


@pytest.mark.req("EF-BIB-013")
def test_auth_declared_requires_login_and_refresh() -> None:
    from shelfhold.sdk.models import AuthApiKey

    auth = AuthApiKey(location="header", name="x-key")
    assert _check(
        _run(FakeMetadataProvider(auth=auth, with_login=True)), "capabilities_implemented"
    )
    assert not _check(
        _run(FakeMetadataProvider(auth=auth, with_login=False)), "capabilities_implemented"
    )


@pytest.mark.req("EF-BIB-013")
def test_sdk_1_2_is_compatible_with_n_minus_1() -> None:
    """D16: MetadataProvider is an addition (SDK 1.2); a 1.1 plugin keeps working."""
    assert SDK_VERSION == "1.3"  # the freeze (L34) is the next minor; 1.2 plugins still load
    assert sdk_supports("1.1") and sdk_supports("1.2") and sdk_supports("1.3")
    assert not sdk_supports("1.4") and not sdk_supports("2.0")
    manifest = FakeMetadataProvider().manifest
    assert manifest.kind is PluginKind.METADATA and manifest.api == 1 and manifest.min_sdk == "1.2"
    # a manifest written by a later 1.x (unknown field) still loads: additions are optional
    data = manifest.model_dump(mode="json") | {"added_in_1_3": True}
    assert MetadataManifest.model_validate(data) == manifest
    too_new = FakeMetadataProvider(min_sdk="1.9")
    assert not _check(_run(too_new), "sdk_compatible")
