"""Guard: what a `req` test cites is reachable from a real entry point (M1a L58).

The tests of the analysis itself use a synthetic package; the two guards at the top run it on the
real tree. Exempt from the `req` marker (tools/traceability-exceptions.toml): they check the
tests, not a product requirement.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from tests.tools_loader import REPO, load_tool

reach = load_tool("reachability")


def test_req_cited_symbols_have_production_caller() -> None:
    orphans = reach.unlisted_orphans(REPO)
    assert not orphans, (
        "symbols cited by a `req` test that no entry point reaches (traced, not wired); wire them "
        "or list them in tools/production-reachability-allowlist.toml with a reason and an "
        "`until`:\n" + "\n".join(f"  {o.symbol}  <- {o.test}" for o in orphans)
    )


def test_req_tests_declare_no_routes() -> None:
    found = reach.route_declarations(REPO)
    assert not found, (
        "a `req` test must exercise create_app() as composed: no route, middleware or `routers=` "
        "of its own:\n" + "\n".join(f"  {line}" for line in found)
    )


def test_reachability_allowlist_is_justified_and_live() -> None:
    entries = reach.load_allowlist(REPO)
    assert reach.check_allowlist(entries) == []
    orphans = {o.symbol for o in reach.find_orphans(REPO)}
    stale = [
        sym
        for e in entries
        for sym in e["symbols"]
        if not any(reach._covered(o, {sym}) for o in orphans)
    ]
    assert not stale, f"allowlist entries that are wired or no longer cited: {stale}"
    routes = reach.load_route_tests(REPO)
    assert reach.check_allowlist(routes) == []
    declaring = set(reach.route_declarations(REPO, listed=True))
    declaring_ids = {line.split(" ")[0] for line in declaring}
    gone = [t for e in routes for t in e["tests"] if t not in declaring_ids]
    assert not gone, f"route tests that declare no route any more: {gone}"


# -- the analysis on a synthetic package ---------------------------------------------------------


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text), encoding="utf-8")
    return tmp_path


_PACKAGE = {
    "backend/src/shelfhold/__init__.py": "",
    "backend/src/shelfhold/main.py": """
        from shelfhold.service import Service, build

        def create_app():
            return build(Service())
    """,
    "backend/src/shelfhold/service.py": """
        from shelfhold.pure import wired_helper

        class Service:
            def run(self):
                return wired_helper()

        def build(service):
            service.run()  # the injected object is only known by the method name
            return service
    """,
    "backend/src/shelfhold/pure.py": """
        __all__ = ["wired_helper", "orphan_helper"]

        def wired_helper():
            return 1

        def orphan_helper():
            return 2

        def calls_orphan():
            return orphan_helper()
    """,
    "backend/tests/test_cites.py": """
        import pytest
        from shelfhold.pure import calls_orphan, orphan_helper, wired_helper

        @pytest.mark.req("EF-X-001")
        def test_wired():
            assert wired_helper() == 1

        @pytest.mark.req("EF-X-001")
        def test_orphan():
            assert orphan_helper() + calls_orphan() == 4

        def test_unmarked():
            from shelfhold.service import Service
    """,
}


def test_orphan_is_reported_and_wired_symbol_through_injection_is_not(tmp_path: Path) -> None:
    root = _tree(tmp_path, _PACKAGE)
    symbols = {o.symbol for o in reach.find_orphans(root)}
    # `__all__` and the definition do not count; a caller that is itself unreached does not either
    assert symbols == {"shelfhold.pure:orphan_helper", "shelfhold.pure:calls_orphan"}


def test_method_of_an_unreached_class_is_not_saved_by_a_homonym(tmp_path: Path) -> None:
    files = {
        "backend/src/shelfhold/__init__.py": "",
        "backend/src/shelfhold/main.py": """
            from shelfhold.live import Live

            def create_app():
                return Live().plan()
        """,
        "backend/src/shelfhold/live.py": """
            class Live:
                def plan(self):
                    return 1
                def unused(self):
                    return 2
        """,
        "backend/src/shelfhold/dead.py": """
            class Dead:
                def plan(self):
                    return 3
        """,
        "backend/tests/test_cites.py": """
            import pytest
            from shelfhold.dead import Dead
            from shelfhold.live import Live

            @pytest.mark.req("EF-X-001")
            def test_dead():
                assert Dead().plan() == 3

            @pytest.mark.req("EF-X-001")
            def test_live():
                assert Live().unused() == 2
        """,
    }
    symbols = {o.symbol for o in reach.find_orphans(_tree(tmp_path, files))}
    # `.plan` is called in production, but on Live: Dead is never built, so neither it nor its
    # `plan` is wired; Live.unused is a method of a reached class that nothing calls
    assert symbols == {"shelfhold.dead:Dead", "shelfhold.dead:Dead.plan"} | {
        "shelfhold.live:Live.unused"
    }


def test_a_method_fetched_by_name_with_getattr_is_used(tmp_path: Path) -> None:
    files = {
        "backend/src/shelfhold/__init__.py": "",
        "backend/src/shelfhold/main.py": """
            from shelfhold.live import Live

            def create_app():
                return getattr(Live(), "optional", None)
        """,
        "backend/src/shelfhold/live.py": """
            class Live:
                def optional(self):
                    return 1
                def forgotten(self):
                    return 2
        """,
        "backend/tests/test_cites.py": """
            import pytest
            from shelfhold.live import Live

            @pytest.mark.req("EF-X-001")
            def test_it():
                assert Live().optional() + Live().forgotten() == 3
        """,
    }
    assert {o.symbol for o in reach.find_orphans(_tree(tmp_path, files))} == {
        "shelfhold.live:Live.forgotten"
    }


def test_symbols_built_by_a_test_helper_count_as_cited(tmp_path: Path) -> None:
    files = {
        "backend/src/shelfhold/__init__.py": "",
        "backend/src/shelfhold/main.py": "def create_app():\n    return 1\n",
        "backend/src/shelfhold/dead.py": "class Dead:\n    pass\n",
        "backend/tests/__init__.py": "",
        "backend/tests/dead_env.py": (
            "from shelfhold.dead import Dead\n\ndef make():\n    return Dead()\n"
        ),
        "backend/tests/test_cites.py": """
            import pytest
            from tests.dead_env import make

            @pytest.mark.req("EF-X-001")
            def test_it():
                assert make()
        """,
    }
    assert {o.symbol for o in reach.find_orphans(_tree(tmp_path, files))} == {"shelfhold.dead:Dead"}


def test_reference_from_an_entry_point_clears_the_orphan(tmp_path: Path) -> None:
    files = dict(_PACKAGE)
    files["backend/src/shelfhold/main.py"] = (
        "from shelfhold.pure import calls_orphan\n"
        "from shelfhold.service import Service, build\n"
        "def create_app():\n    return calls_orphan(), build(Service())\n"
    )
    assert reach.find_orphans(_tree(tmp_path, files)) == []


def test_allowlist_entry_covers_members_and_needs_reason_and_until() -> None:
    allowed = {"shelfhold.m:Klass", "shelfhold.sdk.*"}
    assert reach._covered("shelfhold.m:Klass.attr", allowed)
    assert reach._covered("shelfhold.sdk.testing.kit:run", allowed)
    assert not reach._covered("shelfhold.m:Other", allowed)
    good = {"category": "c", "symbols": ["a:b"], "reason": "why", "until": "M1b L24"}
    assert reach.check_allowlist([good, {**good, "symbols": ["a:c"], "until": "M2"}]) == []
    problems = reach.check_allowlist(
        [
            {"category": "c", "symbols": ["a:b"], "reason": " ", "until": "soon"},
            {**good, "until": "M1b L99"},
            {**good, "category": "public-sdk", "symbols": ["a:d"], "until": "permanent"},
            {**good, "symbols": ["a:e"], "until": "permanent"},  # only some categories
        ]
    )
    # no reason, bad until, duplicate a:b (twice), bad until L99, permanent outside its categories
    assert len(problems) == 5, problems


def test_route_declaration_in_a_req_test_is_reported(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "backend/tests/test_routes.py": """
                import pytest

                @pytest.mark.req("EF-X-001")
                def test_fake_route(app, env):
                    app.add_api_route("/x", lambda: 1)
                    env(routers=[object()])

                @pytest.mark.req("EF-X-001")
                def test_clean(app):
                    assert app
            """
        },
    )
    assert [f.split(" ")[0] for f in reach.route_declarations(root)] == [
        "backend/tests/test_routes.py::test_fake_route"
    ]
