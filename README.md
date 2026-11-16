**Project status: active, pre-release.** Latest stable release: 1.0.1. Next: 1.1.0 (release candidates `v1.1.0-rc.N`). Plan: [ROADMAP.md](ROADMAP.md).

# Shelfhold

**Shelfhold is the workshop of your household's personal digital library, built to sit next to [Kavita](https://www.kavitareader.com/).** It takes care of what your readers don't: fetching the books, comics and manga you are entitled to keep, filing them under a stable identity, normalising them (names, numbering, metadata), then placing them in the libraries your household already reads from, with parental control per shelf.

It is a workshop, not a reader, and not a sharing or hosting service.

## What it does

- Follows series from sources and connected services that **you** configure, and files chapters and volumes into an existing Kavita library without rewriting it.
- Keeps one canonical copy per unit (book, volume, chapter), with open backup and export.
- Serves an OPDS catalogue and a kosync server for e-readers such as KOReader, including a whole series as a single download, and relays reading progress to and from Kavita.
- Works headless (configuration file and environment variables) or through its web interface.
- Stays local: no telemetry, no central service, no embedded AI model.

## What it does not do

- It ships no content and no source: meta-sources come with **no active source**, and there is no preconfigured extension index.
- Protected files are not supported, and the project documents no way around a technical protection.
- It is not a Kavita competitor (the built-in reader is secondary), nor a service for third parties.
- No paid feature, no affiliate link.

Legal posture: [ADR-0015](docs/adr/0015-posture-legale.md) (this is not legal advice).

## Quickstart

You need Docker with Compose v2, on amd64 or arm64.

```sh
mkdir shelfhold && cd shelfhold
curl -fsSLO https://raw.githubusercontent.com/Themlaw/shelfhold/main/compose.simple.yaml
mkdir -p data store && sudo chown 1000:1000 data store
docker compose -f compose.simple.yaml up -d
docker compose -f compose.simple.yaml exec shelfhold shelfhold admin create
```

Then open <http://localhost:8080> and sign in with the administrator you just created. To create it in the browser instead, set `SHELFHOLD_SETUP_TOKEN` in the compose file before the first start (see the comment there).

Next steps:

- Connect Kavita, then add a shelf: [`docs/admin/`](docs/admin/README.md).
- E-readers (OPDS and kosync) need HTTPS: put Shelfhold behind a reverse proxy, set `SHELFHOLD_PUBLIC_URL`, then create a device token in *Settings → Devices*. See [`docs/admin/opds.md`](docs/admin/opds.md) and [`docs/admin/kosync.md`](docs/admin/kosync.md).

### Two compose files

| File | For |
|---|---|
| [`compose.simple.yaml`](compose.simple.yaml) | A first try on a trusted home network: the image, two folders, one port. |
| [`compose.yaml`](compose.yaml) | The hardened reference, and the tested one: read-only root filesystem, all capabilities dropped, `no-new-privileges`, memory and process limits, log rotation, no core dumps, port bound to `127.0.0.1` for a reverse proxy. Optional hardened authentication profile ([`docs/admin/durcir.md`](docs/admin/durcir.md)). |

Both pin `ghcr.io/themlaw/shelfhold:1.0`, which follows the 1.0.x patch releases. Tags: `X.Y.Z`, `X.Y` and `latest` for releases, `edge` for the development image (not for a library you care about). Upgrades: [`docs/admin/mise-a-niveau.md`](docs/admin/mise-a-niveau.md).

The image is multi-stage (Python 3.13 slim, base images pinned by digest), runs as uid 1000, has a health check, and weighs about 96 MB compressed.

## How it is built

Most of the code is written by AI agents driven by the maintainer, under mandatory test-driven development and cross-review. Read [AI_POLICY.md](AI_POLICY.md).

## Documentation

Most of the documentation is in French.

| Topic | Where |
|---|---|
| Administration | [`docs/admin/`](docs/admin/README.md), [`docs/configuration.md`](docs/configuration.md) |
| Requirements | [`docs/spec/01-cahier-des-charges.md`](docs/spec/01-cahier-des-charges.md) |
| Architecture | [`docs/spec/02-architecture.md`](docs/spec/02-architecture.md) |
| Roadmap and TDD method | [`docs/spec/03-roadmap-tdd.md`](docs/spec/03-roadmap-tdd.md) |
| Repository, CI, releases | [`docs/spec/04-structure-repo.md`](docs/spec/04-structure-repo.md), [`docs/releasing.md`](docs/releasing.md) |
| Decisions | [`docs/adr/`](docs/adr/README.md) |
| Governance | [GOVERNANCE.md](GOVERNANCE.md), [`docs/governance/`](docs/governance/portes.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Security | [SECURITY.md](SECURITY.md) |
| Changes | [CHANGELOG.md](CHANGELOG.md) |

## License

AGPL-3.0-or-later, with a **provisional** additional permission for plugins (see [LICENSE](LICENSE)). The `sdk/` folder is under the MIT license. Trademarks: [TRADEMARKS.md](TRADEMARKS.md).
