"""SDK data contracts: JSON round trip of every exported type, content_trust default."""

import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from shelfhold.sdk import connected, export_target, fetcher, models, refs, source

OPAQUE = "opaque-handle"
URL = "https://a.invalid/t"


def _manifest(**extra: Any) -> source.SourceManifest:
    return source.SourceManifest(
        key="sample",
        kind=models.PluginKind.SOURCE,
        version="1.0.0",
        api=models.API_VERSION,
        name="Sample",
        langs=["en"],
        content_kinds={"images", "file"},
        url_patterns=[r"^https://sample\.invalid/"],
        capabilities=source.Capability.SEARCH | source.Capability.RANGED_CONTENT,
        rate_limit=models.RateLimit(requests_per_second=2.0, concurrency=1),
        **extra,
    )


def _series() -> refs.SeriesRef:
    return refs.SeriesRef(source_key="sample", remote_id="series/1", title="A series")


def _unit(n: int = 1) -> refs.UnitInfo:
    return refs.UnitInfo(
        remote_unit_id=f"sample:ch-{n}",
        source_order=n,
        number=str(n),
        number_sort=Decimal(n) + Decimal("0.5"),
        title=None,
        volume=None,
        volume_label="Vol.1",
        volume_closed=True,
        team=None,
        lang="en",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def _image_request() -> refs.ImageRequest:
    return refs.ImageRequest(
        url="https://sample.invalid/p/1.png",
        method="POST",
        body=b"\x00\xffbinary",
        expires_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def _asset() -> refs.FileAsset:
    return refs.FileAsset(request=_image_request(), mime="application/epub+zip", filename="v1.epub")


def _range() -> refs.RangeRef:
    return refs.RangeRef(series=_series(), first=_unit(1), last=_unit(8), volume_label="Vol.1")


def _samples() -> dict[type[BaseModel], BaseModel]:
    samples: list[BaseModel] = [
        models.DomainRule(pattern="*.sample.invalid", purpose="api"),
        models.Permissions(domains=[models.DomainRule(pattern="sample.invalid", purpose="pages")]),
        models.RateLimit(requests_per_second=1.5, concurrency=2),
        models.PluginManifest(
            key="plain", kind=models.PluginKind.EXPORT_TARGET, version="1", api=1, name="Plain"
        ),
        models.Cursor(token=OPAQUE),
        models.Page[refs.SeriesRef](items=[_series()], next=models.Cursor(token=OPAQUE)),
        models.FilterSpec(
            key="sort",
            kind="group",
            label="Sort",
            options=[("a", "A")],
            children=[models.FilterSpec(key="c", kind="checkbox", label="C", default=True)],
        ),
        models.AuthBasic(),
        models.AuthForm(fields=["user", "password"]),
        models.AuthApiKey(location="header", name="X-Key"),
        models.AuthCookieImport(domains=["sample.invalid"]),
        models.AuthDeviceCode(device_url="https://a.invalid/d", token_url=URL, scopes=["r"]),
        models.AuthInteractive(reason="captcha"),
        models.SessionState(expires_at=datetime(2026, 1, 2, tzinfo=UTC), opaque={"k": [1, 2]}),
        _manifest(),
        source.SourceTestCase(name="smoke", cassette="cassettes/smoke.json"),
        source.SubSource(
            source_key="sample:1", name="Sub", lang="mul", content_warning="mixed", limit_key="up-1"
        ),
        _series(),
        refs.SeriesInfo(ref=_series(), title="A series", external_ids={"isbn": "x"}),
        _unit(),
        _image_request(),
        refs.ScrambleInfo(processor="descramble", params={"n": 3}),
        refs.PageRef(index=0, token=OPAQUE, scrambled=refs.ScrambleInfo(processor="p", params={})),
        refs.ImagePages(pages=[refs.PageRef(index=0, token=OPAQUE)]),
        refs.TextContent(xhtml="<p>x</p>", assets=[_image_request()]),
        _asset(),
        refs.UnitRef(series=_series(), unit=_unit()),
        _range(),
        refs.RangedPayload(
            asset=_asset(),
            covers=["sample:ch-1", "sample:ch-2"],
            bounds={"sample:ch-1": ("start", "end")},
            volume_closed=False,
        ),
        connected.Ticket(token=OPAQUE, target_kind="range"),
        connected.ServiceHealth(ok=False, version="1.2.3", detail="extension not loaded"),
        connected.ReleaseResult(released=True),
        fetcher.Response(status=200, headers={"x": "y"}, body=b"\x00\x01"),
        fetcher.DownloadedFile(sha256="0" * 64, size=3, content=b"abc"),
        fetcher.CookieJarView(cookies={"sid": "snapshot"}),
        export_target.ApiCall(method="GET", path="/api/x", query={"a": 1}, auth="target_admin"),
        export_target.ContentRef(kind="export_item", ref=OPAQUE),
        export_target.TargetCapabilities(
            media_kinds={"manga"},
            formats={"cbz"},
            transport="tree",
            egress="foyer",
            can_update=True,
            can_delete=True,
            confirms_arrival=True,
            reconcilable=True,
            accepted_content_types=["cbz"],
            sidecars={"comicinfo"},
            path_scoped_scan=True,
            access_partitioning="library",
            accepted_image_types={"jpeg"},
            events="signalr",
            collections="api",
            progress="api",
            manage_libraries=True,
            naming=export_target.NamingConstraints(
                max_segment_bytes=255, max_path_bytes=4096, forbidden="/", case_insensitive=False
            ),
            name_normalizer="kavita-v1",
        ),
        export_target.FsOp(op="rename", rel_path="a/b", dst_rel_path="a/c"),
        export_target.NamingConstraints(
            max_segment_bytes=255, max_path_bytes=4096, forbidden="/\\", case_insensitive=False
        ),
    ]
    return {type(m): m for m in samples}


def _exported_models() -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    for module in (models, refs, source, connected, export_target, fetcher):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(cls, BaseModel)
                and cls.__module__ == module.__name__
                and cls not in (models.SdkModel, models.StrictSdkModel)
                and not cls.__pydantic_generic_metadata__["parameters"]  # unparametrised generic
            ):
                found.append(cls)
    return found


@pytest.mark.req("EF-SRC-001")
def test_sdk_models_json_roundtrip() -> None:
    samples = _samples()
    for instance in samples.values():
        again = type(instance).model_validate_json(instance.model_dump_json())
        assert again == instance, type(instance).__name__
    # a new type of the SDK must come with a sample: the contract is "every type"
    missing = [cls.__name__ for cls in _exported_models() if cls not in samples]
    assert not missing, missing


@pytest.mark.req("EF-SRC-001")
def test_sdk_json_forms_bytes_base64_decimal_string() -> None:
    wire = json.loads(_image_request().model_dump_json())
    assert wire["body"] == "AP9iaW5hcnk="  # bytes travel as base64
    assert json.loads(_unit(2).model_dump_json())["number_sort"] == "2.5"  # Decimal as a string
    cookies = json.loads(fetcher.CookieJarView(cookies={"a": "b"}).model_dump_json())
    assert cookies == {"cookies": {"a": "b"}}  # a snapshot, never a live jar


@pytest.mark.req("EF-SRC-001")
def test_sdk_manifest_capabilities_travel_as_names() -> None:
    wire = json.loads(_manifest().model_dump_json())
    assert sorted(wire["capabilities"]) == ["RANGED_CONTENT", "SEARCH"]
    assert sorted(wire["content_kinds"]) == ["file", "images"]
    with pytest.raises(ValidationError):
        source.SourceManifest.model_validate({**wire, "capabilities": ["NOPE"]})


@pytest.mark.req("EF-SRC-001", "EF-BIB-051")
def test_sdk_manifest_content_trust_defaults_untrusted() -> None:
    manifest = _manifest()
    assert manifest.content_trust == "untrusted"
    wire = json.loads(manifest.model_dump_json())
    wire.pop("content_trust")
    assert source.SourceManifest.model_validate(wire).content_trust == "untrusted"
    assert _manifest(content_trust="publisher").content_trust == "publisher"
    with pytest.raises(ValidationError):
        _manifest(content_trust="trusted")


@pytest.mark.req("EF-SRC-001")
def test_sdk_manifest_carries_the_v1_fields() -> None:
    fields = set(source.SourceManifest.model_fields)
    expected = {
        "key", "version", "api", "langs", "content_kinds", "url_patterns", "capabilities",
        "settings_schema", "rate_limit", "terms_url", "content_warning", "content_trust",
    }  # fmt: skip
    assert expected <= fields


@pytest.mark.req("EF-SRC-001")
def test_unknown_fields_are_ignored_in_manifests_but_refused_in_host_plans() -> None:
    wire = json.loads(_manifest().model_dump_json())
    wire["added_in_a_later_api"] = {"x": 1}
    wire["rate_limit"]["burst"] = 3
    assert source.SourceManifest.model_validate(wire).key == "sample"  # N reads N-1 and back
    assert not hasattr(source.SourceManifest.model_validate(wire), "added_in_a_later_api")
    with pytest.raises(ValidationError):
        export_target.FsOp.model_validate({"op": "mkdir", "rel_path": "a", "surprise": 1})
    with pytest.raises(ValidationError):
        export_target.ApiCall.model_validate({"method": "GET", "path": "/", "auth": "none", "x": 1})


@pytest.mark.req("EF-SRC-001")
def test_export_target_module_warns_that_it_is_provisional_until_m4() -> None:
    assert "UNSTABLE" in (export_target.__doc__ or "")


@pytest.mark.req("ENF-LEG-010")
def test_typos_exceptions_are_exact_identifiers_not_global_words() -> None:
    import tomllib
    from pathlib import Path

    config = tomllib.loads((Path(__file__).parents[3] / "typos.toml").read_text(encoding="utf-8"))
    words = config["default"]["extend-words"]
    assert "ser" not in words and "opf" not in words
    assert config["default"]["extend-identifiers"] == {"ser_json_bytes": "ser_json_bytes"}
