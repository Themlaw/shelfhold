"""M3 L09 (EF-USR-006, EF-USR-016, ENF-SEC-010): a child sees nothing that is not rated, nothing
above her ceiling, on any surface, and a Work she may not see is exactly like a Work that does not
exist.

Everything goes through the real application (`create_app()`, real sessions, real CSRF). The
single rule is a `ViewerScope` applied to the query: nothing is filtered afterwards, so a page, a
count or a cursor cannot carry a hidden row out.

The meta-test holds the list of routes: a route of the OpenAPI document with no child case here
fails the suite. A route that a child can use (OPEN) names the test that attacks it.
"""

from __future__ import annotations

import base64
import hashlib
import sys
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.routing import APIWebSocketRoute, iter_route_contexts
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute

from shelfhold.catalog.public import (
    count_visible_works,
    list_visible_works,
    visible_edition_ids,
    visible_work_ids,
)
from shelfhold.config import Settings
from shelfhold.events.activity import ActivityEvent, Viewer
from shelfhold.events.projection import project_event
from shelfhold.identity.public import local_auth, reader_scope, scope_of
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env, run_cli
from tests.visibility_env import (
    HIDDEN_TITLES,
    VISIBLE_TITLES,
    Seed,
    add_profile,
    seed_catalogue,
    work_is_visible,
)

ULID = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
MISSING = "01HZZZZZZZZZZZZZZZZZZZZZZM"
SEARCH = "/api/v1/search"
ME = "/api/v1/me"

# -- the child case of every route of the OpenAPI document -------------------------------------
# PUBLIC: needs no session and serves no catalogue data. OWN: the caller's own account, never
# catalogue data. OPEN: a child may use it and sees only what the filter lets through; the value
# is the surface that `test_unrated_invisible_to_child_on_all_surfaces` attacks. CLOSED: refused
# to a child (403), by session and by key.

PUBLIC = (
    "GET /api/v1/source",
    "GET /robots.txt",
    "GET /login",
    "GET /login/passkey.js",
    "POST /api/v1/auth/login",
    "GET /api/v1/setup/status",  # ENF-UX-001: a new instance says it waits for its first account
    "POST /api/v1/setup/admin",  # ...and makes it, behind the secret of the compose
    "POST /api/v1/auth/passkeys/login",
    "POST /api/v1/auth/passkeys/login/options",
    # M5 L03: the OpenID Connect sign-in is anonymous by nature (a browser sent to the provider and
    # back); absent (404) unless configured, never a catalogue datum
    "GET /api/v1/auth/oidc/login",
    "GET /api/v1/auth/oidc/callback",
    # ...and its confirmation page is the same-site page the callback sends the browser to, whose
    # `Strict` session cookie did not come along: bound to the flow cookie, it writes nothing
    "GET /api/v1/auth/oidc/link/confirm",
    "GET /healthz",
    "GET /readyz",
)
OWN = (
    "GET /api/v1/me",
    "GET /api/v1/about",
    "POST /api/v1/auth/logout",
    "GET /api/v1/auth/passkeys",
    "GET /api/v1/auth/oidc/identities",  # M5 L03: the links of her own account, never a `sub`
    "POST /api/v1/auth/passkeys",
    "POST /api/v1/auth/passkeys/options",
    "POST /api/v1/auth/passkeys/reauth/options",
    "DELETE /api/v1/auth/passkeys/{passkey_id}",
    "GET /api/v1/auth/sessions",  # M3 L07: her own sessions, a label and dates, no catalogue data
    "POST /api/v1/auth/sessions/revoke-others",
    "DELETE /api/v1/auth/sessions/{session_id}",
    "POST /api/v1/auth/reauth",
    "GET /api/v1/auth/session",
    "GET /api/v1/me/preferences",
    "PATCH /api/v1/me/preferences",
    "GET /api/v1/me/title-languages",
    "PUT /api/v1/me/title-languages",
    # M3 L14: how she wants to be told, her own row only (no identifier names another profile)
    "GET /api/v1/me/notifications",
    "PUT /api/v1/me/notifications",
    "GET /api/v1/me/visibility",
    "PUT /api/v1/me/visibility",
    # M3 L35: her own requests, titled by what she was shown; a child asks by her reference only
    # (no Work identifier, so no title of the catalogue can come back), and another profile's
    # request is exactly an absent one (tests/integration/test_requests_http.py)
    "GET /api/v1/requests",
    "POST /api/v1/requests",
    "GET /api/v1/requests/{request_id}",
    "POST /api/v1/requests/{request_id}/cancel",
)
# DEVICE (M4 L46): behind a device token and nothing else, the credential of an e-reader. A session,
# a child's or an adult's, and an API key are refused (401); the token of a child reads as that
# child and the route answers no catalogue data. The attacks are in `test_l46_device_tokens.py`;
# the feed (L48, `tests/integration/test_l48_opds.py`) joins here with its own surface `opds`. The
# kosync routes of L47 that are not GET (`PUT /kosync/syncs/progress`, `POST /kosync/users/create`)
# have their child cases in `test_l47_kosync.py` (a child's PUT on a Work above her ceiling is a 202
# that stores nothing, a session and a key are refused); the read of a position is here, where it
# answers a child with `{}` for any fingerprint and no catalogue data.
KOSYNC = ("GET /kosync/users/auth", "GET /kosync/syncs/progress/{document}")
KOSYNC_WRITE = ("PUT /kosync/syncs/progress", "POST /kosync/users/create")
OPDS = (
    "GET /opds",
    "GET /opds/opensearch.xml",
    "GET /opds/shelves",
    "GET /opds/shelves/{shelf_id}",
    "GET /opds/series",
    "GET /opds/series/{series_id}",
    "GET /opds/works",
    "GET /opds/works/{work_id}",
    "GET /opds/recent",
    "GET /opds/selections",  # M5 L21: the profile's own selections, evaluated in her scope
    "GET /opds/selections/{selection_id}",
    "GET /opds/search",
    "GET /opds/covers/{content_hash}/{size}",
    "GET /opds/editions/{edition_id}/complete",
    "GET /opds/kepub/{item_id}",
    "GET /opds/bundles/{version_id}",
    "GET /opds/pse/{item_id}/{page}",  # M5 L18: OPDS-PSE, a page of an exported volume
    # M5 L19: the OPDS 2.0 JSON feed, the same queries as the 1.2 one (the crawl below walks both)
    "GET /opds/v2",
    "GET /opds/v2/shelves",
    "GET /opds/v2/shelves/{shelf_id}",
    "GET /opds/v2/series",
    "GET /opds/v2/series/{series_id}",
    "GET /opds/v2/works",
    "GET /opds/v2/works/{work_id}",
    "GET /opds/v2/recent",
    "GET /opds/v2/selections",
    "GET /opds/v2/selections/{selection_id}",
    "GET /opds/v2/search",
)
# LITE (M5 L17a): the pages of `/lite`, behind a cookie that a device token's secret opened. Every
# route is hers when it shows catalogue data, and answers like OPDS: a hidden thing is an absent
# one. The two forms (sign-in, sign-out) hold no catalogue data. The attacks:
# `tests/integration/test_m5_l17a_lite.py`.
LITE = (
    "GET /lite",
    "GET /lite/sign-in",
    "POST /lite/sign-in",
    "POST /lite/sign-out",
    "GET /lite/shelves",
    "GET /lite/shelves/{shelf_id}",
    "GET /lite/series",
    "GET /lite/series/{series_id}",
    "GET /lite/works",
    "GET /lite/works/{work_id}",
    "GET /lite/recent",
    "GET /lite/search",
    "GET /lite/covers/{content_hash}/{size}",
    "GET /lite/editions/{edition_id}/complete",
    "GET /lite/kepub/{item_id}",
    "GET /lite/{rest:path}",
)
DEVICE = (*KOSYNC, *OPDS)
DEVICE_ALL = (*DEVICE, *KOSYNC_WRITE)
OPEN = {
    **{route: "lite" for route in LITE},  # M5 L17a
    "GET /api/v1/activity": "activity",
    "GET /api/v1/events": "sse",
    "GET /api/v1/me/state": "me_state_list",
    "GET /api/v1/me/state/{work_id}": "me_state_get",
    "PUT /api/v1/me/state/{work_id}": "me_state_put",
    "GET /api/v1/me/works/{work_id}/title": "me_title",
    "GET /api/v1/search": "search",
    "GET /api/v1/works": "works_since",  # M3 L11: the list of Works and its `?since=` removals
    # M3 L24: a cover's thumbnail, by session only; hers when an Edition she may see points at it
    "GET /api/v1/thumbnails/{content_hash}/{size}": "thumbnail",
    "GET /content/{ticket}/{path:path}": "content",  # M5 L05a: the bytes of a book, by ticket
    # M5 L06: what the reader opens (pages or spine, a ticket in each link); only for a carrier,
    # or a Unit, she may see: the rest is one 404 with the answer for an absent one
    # M5 L14: « Reprendre la lecture » of the home: her own progress, narrowed by the filter of
    # every surface; a hidden series, though a pull reports it, is exactly absent
    # (`test_m5_l14_continue_reading.py`)
    "GET /api/reader/continue": "continue_reading",
    "GET /api/reader/{carrier_type}/{carrier_id}/publication": "reader",
    # M5 L07: where the reader is, its history, and the choice between two places: hers only, on a
    # carrier she may see; the rest is the answer for an absent one (`test_m5_l07_positions.py`)
    "GET /api/reader/{carrier_type}/{carrier_id}/position": "reader",
    "GET /api/reader/{carrier_type}/{carrier_id}/positions": "reader",
    "PUT /api/reader/positions": "reader",
    "POST /api/reader/conflicts/{conflict_id}/resolve": "reader",
    # M5 L13: her reader settings; a series she may not see answers like an absent one, and no
    # setting widens what parental control hides (`test_m5_l13_settings.py`)
    "GET /api/reader/settings": "reader",
    "PUT /api/reader/settings": "reader",
    # M6 L18: the read marks of an Edition and the undo of one: hers only, and only on an Edition
    # she may see — a hidden one answers like one with no mark (`test_l18_undo_read_mark.py`)
    "GET /api/reader/read-marks": "reader",
    "POST /api/reader/read-marks/{unit_id}/undo": "reader",
    # M4 L23: the official links of an Edition she may see; a hidden one is an absent one
    "GET /api/v1/editions/{edition_id}/support-author": "support_author",
    # M4 L18a: the collections and reading lists, made of the Works she may see and a list of her
    # own; another profile's list is an absent one (`test_m4_l18a_collections.py`)
    "GET /api/v1/collections": "collections",
    "POST /api/v1/collections": "collections",
    "POST /api/v1/collections/import-cbl": "collections",
    "GET /api/v1/collections/{collection_id}": "collections",
    "DELETE /api/v1/collections/{collection_id}": "collections",
    "POST /api/v1/collections/{collection_id}/items": "collections",
    "DELETE /api/v1/collections/{collection_id}/items/{item_id}": "collections",
    # M5 L21: the saved selections: a stored query, evaluated in her scope at each read; another
    # profile's selection is an absent one (`test_m5_l21_saved_selections.py`)
    "GET /api/v1/selections": "selections",
    "POST /api/v1/selections": "selections",
    "GET /api/v1/selections/{selection_id}": "selections",
    "PUT /api/v1/selections/{selection_id}": "selections",
    "DELETE /api/v1/selections/{selection_id}": "selections",
    # M4 L19: the relations of a Work she may see, and of those the ones whose other end she may
    # see too (`test_m4_l19_relations.py`); making and removing one is the admin's (CLOSED)
    "GET /api/v1/works/{work_id}/relations": "relations",
}
CLOSED = (
    "GET /api/v1/openapi.json",
    "GET /api/v1/maintenance",  # M3 L13: the mode, the window and the queue are the admin's
    "POST /api/v1/maintenance/enter",
    "POST /api/v1/maintenance/exit",
    "POST /api/v1/queue/pause",
    "POST /api/v1/queue/resume",
    "POST /api/v1/jobs/{job_id}/cancel",
    "POST /api/v1/jobs/{job_id}/priority",
    "POST /api/v1/jobs/{job_id}/replay",
    "POST /api/v1/acquire",
    "POST /api/v1/activity/{ref}/unmask",
    "POST /api/v1/adoption/{destination_id}/confirm-migration",
    "POST /api/v1/adoption/{destination_id}/follow-from",
    "GET /api/v1/adoption/{destination_id}/migration",
    "POST /api/v1/adoption/{destination_id}/pair",
    "POST /api/v1/adoption/{destination_id}/pair-auto",
    "GET /api/v1/adoption/{destination_id}/pairing",
    "POST /api/v1/adoption/{destination_id}/repatriate",
    "GET /api/v1/config",
    "PATCH /api/v1/config",
    "POST /api/v1/coverage/resplit/apply",
    "POST /api/v1/coverage/resplit/propose",
    "POST /api/v1/deletions",
    "POST /api/v1/deletions/preview",
    # M3 L26: the reclaim actions and the lift are the admin's (reserved: a fresh session or the
    # host CLI); the sheet's size check shows the free space of the host, which is not hers either
    "POST /api/v1/trash/empty",
    "POST /api/v1/tombstones/{seq}/lift",
    "GET /api/v1/tombstones",
    "POST /api/v1/library/space-check",
    "POST /api/v1/detach",
    # M3 L45: the destinations and the full detachment (reserved: a fresh admin session, a typed
    # phrase) are the admin's: a child is refused by session and by key, before any service
    "GET /api/v1/destinations",
    # M4 L33a: the servers a shelf can be bound to are the admin's (a session, never a key)
    "GET /api/v1/targets",
    "POST /api/v1/destinations/{destination_id}/removal",
    "POST /api/v1/destinations/{destination_id}/removal-preview",
    "POST /api/v1/targets/{target_id}/detach",
    "POST /api/v1/targets/{target_id}/detach-preview",
    # M4 L46b: pairing an e-reader is for an adult's session (reserved to create); a child is
    # refused by session and by key, before any service
    "GET /api/v1/device-tokens",
    "POST /api/v1/device-tokens",
    "GET /api/v1/device-tokens/setup",
    "DELETE /api/v1/device-tokens/{public_id}",
    # M5 L03: linking an identity provider to an account is an adult's reserved action (the
    # admin's for another account): a child is refused by session and by key
    "POST /api/v1/auth/oidc/link",
    "POST /api/v1/auth/oidc/link/confirm",
    "PUT /api/v1/profiles/{profile_id}/oidc",
    # M5 L04: unlinking, and proving oneself again at the provider: the same rule
    "DELETE /api/v1/auth/oidc/identities",
    "DELETE /api/v1/profiles/{profile_id}/oidc",
    "POST /api/v1/auth/oidc/reauth",
    # M5 L02: the registry of devices, its rotation and its removal: the same rule
    "GET /api/v1/devices",
    "POST /api/v1/devices/{device_id}/rotate",
    "DELETE /api/v1/devices/{device_id}",
    # M6 L18: the undo of a child's mark is the administrator's door, never a child's
    "POST /api/reader/read-marks/{unit_id}/undo/for-child/{user_id}",
    "GET /api/v1/diagnostic/bundle",  # M3 L46: the bundle is the admin's alone
    "GET /api/v1/editions",
    "POST /api/v1/editions/{edition_id}/add-directly",
    "GET /api/v1/editions/{edition_id}/backfill",
    "POST /api/v1/editions/{edition_id}/backfill",
    "POST /api/v1/editions/{edition_id}/check",
    "POST /api/v1/editions/{edition_id}/gaps/ack",  # M3 L43: the sheet's doors, all the admin's
    "GET /api/v1/editions/{edition_id}/sheet",
    "GET /api/v1/editions/{edition_id}/bundle",  # M4 L49b: the slice of a block, the admin's
    "POST /api/v1/editions/{edition_id}/bundle-slice",
    "POST /api/v1/editions/{edition_id}/bindings/order",
    "POST /api/v1/editions/{edition_id}/upstream-status",
    "POST /api/v1/editions/{edition_id}/preferred-team",
    "POST /api/v1/editions/{edition_id}/preferred",  # M4 L15: the admin's, like the team
    "POST /api/v1/editions/{edition_id}/bindings/{binding_id}/pause",  # M4 L23: the admin's act
    "POST /api/v1/attachments/{attach_id}/plan",
    "POST /api/v1/attachments/{attach_id}/confirm",
    "POST /api/v1/editions/{edition_id}/external-ids/renumber",
    "POST /api/v1/editions/{edition_id}/fields/{field}/lock",
    "POST /api/v1/editions/{edition_id}/fields/{field}/unlock",
    "POST /api/v1/editions/{edition_id}/language",
    "POST /api/v1/editions/{edition_id}/series-name",
    "POST /api/v1/editions/{edition_id}/shelf",
    "GET /api/v1/editions/{edition_id}/shelf-proposal",
    "GET /api/v1/export-items/{item_id}",
    "DELETE /api/v1/follows",
    "POST /api/v1/follows",
    "GET /api/v1/health/page",
    "GET /api/v1/intents",
    "POST /api/v1/intents/{intent_id}/resolve",
    "GET /api/v1/jobs",
    "GET /api/v1/jobs/{job_id}",
    "POST /api/v1/jobs/{job_id}/retry",
    "GET /api/v1/merge-suggestions/works",
    # M3 L22: merging Works, "Séparer", the exported-series plan, the Edition merge plan and what
    # a merge of contributors proposes: reserved to a fresh admin session
    "POST /api/v1/works/merge",
    "POST /api/v1/works/merges/{merge_id}/separate",
    "POST /api/v1/sources/{source_key}/migrate-ids",  # M3 L27: reserved
    "POST /api/v1/series-reassignments",
    "POST /api/v1/works/{work_id}/series",
    "POST /api/v1/editions/merge",
    "POST /api/v1/contributors/merge-proposals",
    "POST /api/v1/merge-suggestions/works/scan",
    "POST /api/v1/missing/{destination_id}/{export_item_id}/resolve",
    "POST /api/v1/plans",
    "POST /api/v1/plans/naming-preview",
    "POST /api/v1/plans/renames",
    "POST /api/v1/plans/{plan_id}/apply",
    "GET /api/v1/plugins/{key}/attestations",
    "POST /api/v1/plugins/{key}/attestations",
    "DELETE /api/v1/plugins/{key}/attestations/{name}",
    # M6 L06 (D13, EF-SRC-019): the guard of the third-party plugins is the admin's, on a fresh
    # session: the list says why a plugin is not loaded, enabling an isolated one is reserved
    "GET /api/v1/plugins",
    "POST /api/v1/plugins/{name}/enabled",
    "DELETE /api/v1/plugins/{name}/enabled",
    "POST /api/v1/probe",
    "GET /api/v1/profiles",
    "POST /api/v1/profiles",
    "DELETE /api/v1/profiles/{profile_id}",
    "GET /api/v1/profiles/{profile_id}/export",
    "PATCH /api/v1/profiles/{profile_id}",
    "GET /api/v1/profiles/{profile_id}/source-policies",
    "PUT /api/v1/profiles/{profile_id}/source-policies/{source_key}",
    "PUT /api/v1/profiles/{profile_id}/family-search",
    "PUT /api/v1/profiles/{profile_id}/kavita-account",  # M3 L10: reserved, a name, never a token
    # M4 L12 (EF-USR-012): a member's Kavita key: her own, a parent's for a child; never a child's
    "GET /api/v1/profiles/{user_id}/destination-accounts",
    "GET /api/v1/profiles/{user_id}/kavita-destinations",  # M4 L33a: what a key may be linked to
    "GET /api/v1/sync-status",  # M4 L33b: the sync state per member, an adult session only
    "PUT /api/v1/profiles/{user_id}/destination-accounts/{shelf_destination_id}",
    "DELETE /api/v1/profiles/{user_id}/destination-accounts/{shelf_destination_id}",
    # M4 L39 (EF-INT-021): the destination of an adult's own notices; a child has none
    "GET /api/v1/me/notification-destination",
    "PUT /api/v1/me/notification-destination",
    "DELETE /api/v1/me/notification-destination",
    "GET /api/v1/audit/kavita-accounts",  # M3 L10: the audit of the Kavita accounts, the admin's
    "POST /api/v1/audit/kavita-accounts",
    "GET /api/v1/audit/komga-accounts",  # L22: the Komga accounts, the admin's on demand
    "POST /api/v1/audit/komga-accounts",
    "PUT /api/v1/profiles/{profile_id}/visibility",
    "POST /api/v1/reconcile/adopt-after-restore",
    "POST /api/v1/reconcile/forget-root",
    "POST /api/v1/restore/acknowledge",
    "GET /api/v1/revisions",
    "POST /api/v1/revisions/apply",
    "POST /api/v1/revisions/force",
    "POST /api/v1/roots/{root}/reinit",
    "POST /api/v1/roots/{root}/restored",
    "POST /api/v1/roots/{root}/takeover",
    "POST /api/v1/secrets/import",
    "POST /api/v1/series/{export_series_id}/resume",
    "GET /api/v1/trash",  # M3 L48: the bin is the admin's
    "GET /api/v1/devices/connect",  # M3 L48: Kavita's entry points, the admin's
    "POST /api/v1/editions/{edition_id}/rating",  # M3 L48: one Edition alone, reserved `rate`
    "GET /api/v1/setup/state",  # M3 L39: the first-run wizard is the admin's, on a fresh session
    "POST /api/v1/setup/adoption/confirm",
    "POST /api/v1/setup/keyring-export",  # M3 L40: the keyring leaves only as an age file
    # M4 L18b: the admin's word that a profile's list may be promoted in Kavita; refused to a
    # child and to a key of hers (`test_m4_l18b_attacks.py`)
    "PUT /api/v1/collections/{collection_id}/kavita-public",  # reserved: collection.kavita_promote
    "DELETE /api/v1/collections/{collection_id}/kavita-public",
    "PUT /api/v1/setup/sources/family",  # M3 L40 (D13): which sources a child's search reaches
    "POST /api/v1/setup/adoption/preview",
    "GET /api/v1/setup/adoption/preview/{job_id}",  # M4 L36: the preview is a task, admin only
    "POST /api/v1/setup/library",
    "POST /api/v1/setup/shelf",  # ENF-UX-001: the wizard makes the shelf linked to Kavita
    "POST /api/v1/setup/probe",
    "POST /api/v1/shelves",
    "POST /api/v1/shelves/precreate",
    "GET /api/v1/shelves",  # M3 L11: the list of shelves and its `?since=` removals
    "GET /api/v1/shelves/withdrawals",
    "POST /api/v1/shelves/withdrawals",
    "POST /api/v1/shelves/{shelf_id}/allowed-profiles",
    "GET /api/v1/shelves/{shelf_id}/destinations",  # M4 L06a: the bindings, the admin's
    "POST /api/v1/shelves/{shelf_id}/destinations",
    "POST /api/v1/shelves/{shelf_id}/max-age",
    "POST /api/v1/shelves/{shelf_id}/skip-read-upgrades",
    "POST /api/v1/sources",
    "POST /api/v1/sources/suwayomi/recheck",
    "POST /api/v1/sources/suwayomi/reconnect",
    "POST /api/v1/sources/suwayomi/secret",
    "PUT /api/v1/sources/{key}/cookies",
    "DELETE /api/v1/sources/{key}/secrets/{name}",
    "PUT /api/v1/sources/{key}/secrets/{name}",
    "GET /api/v1/sources/{key}/settings",
    "PATCH /api/v1/sources/{key}/settings",
    "GET /api/v1/sources/{source_key}/filters",
    "GET /api/v1/sources/{source_key}/search",
    "GET /api/v1/sources/{source_key}/series/{remote_id}/estimate",  # M3 L42
    "GET /api/v1/stats",
    # M4 L29 (EF-INT-019, EF-INT-020): the administrator's alone, by session and by key; the
    # attacks (child, member, read-only key, IDOR) are in `test_m4_l29_activity_dashboard.py`
    "GET /api/v1/ops/activity-history",
    "GET /api/v1/ops/blocklist",
    "POST /api/v1/ops/blocklist",
    "DELETE /api/v1/ops/blocklist/{block_id}",
    "GET /api/v1/ops/storage-dashboard",
    # M4 L24 (EF-INT-005): webhooks, the administrator's alone (writes: a reserved action); the
    # attacks (child, member, keys, IDOR) are in `test_m4_l24_webhook_routes.py`
    "GET /api/v1/webhooks",
    "POST /api/v1/webhooks",
    "GET /api/v1/webhooks/{webhook_id}",
    "DELETE /api/v1/webhooks/{webhook_id}",
    "POST /api/v1/webhooks/{webhook_id}/rotate",  # M6 L23a: the secret's rotation, reserved
    "GET /api/v1/webhooks/{webhook_id}/deliveries",  # M6 L23b: what the screen shows, the admin's
    "GET /metrics",  # M4 L25: a key of scope `metrics` alone
    "GET /api/v1/status",
    "POST /api/v1/store/reinit",
    "POST /api/v1/targets",
    "POST /api/v1/targets/native-folder",  # M4 L07: a reserved action (config.path_map)
    "POST /api/v1/targets/komga",  # M4 L46: a reserved action (target.add_komga)
    "POST /api/v1/targets/cwa",  # M4 L46: a reserved action (config.path_map)
    "POST /api/v1/targets/kobo",  # M6 L16: a reserved action (config.path_map)
    "POST /api/v1/destinations/{destination_id}/sync-progress",  # M4 L46: reserved
    "POST /api/v1/targets/{target_id}/recheck",
    "POST /api/v1/targets/{target_id}/reconnect",
    "POST /api/v1/targets/{target_id}/rotate-key",
    "GET /api/v1/to-rate",
    "GET /api/v1/to-shelve",
    "POST /api/v1/tombstones/accept-lost-keyring",
    "POST /api/v1/tombstones/flush",
    "POST /api/v1/tombstones/set-aside-journal",
    "POST /api/v1/units/{unit_id}/purge",
    "POST /api/v1/units/{unit_id}/restore",
    "POST /api/v1/editions/{edition_id}/source-migrations",  # M4 L27: a reserved action
    "GET /api/v1/editions/{edition_id}/source-migrations/{migration_id}",
    "POST /api/v1/editions/{edition_id}/source-migrations/{migration_id}/validation",
    "POST /api/v1/editions/{edition_id}/source-migrations/{migration_id}/application",
    "POST /api/v1/works/{work_id}/relations",  # M4 L19: the admin's (session or `admin` key)
    "DELETE /api/v1/works/{work_id}/relations/{kind}/{to_work_id}",
    "POST /api/v1/works/rating-batch",
    "POST /api/v1/works/{work_id}/purge",
    "POST /api/v1/works/{work_id}/rating",
    "GET /api/v1/requests/queue",  # M3 L35: what waits for an admin
    "GET /api/v1/requests/{request_id}/approval",
    "POST /api/v1/requests/{request_id}/approve",
    "POST /api/v1/requests/{request_id}/reject",
    "POST /api/v1/requests/{request_id}/not-found",
    "POST /api/v1/bulk/rate/preview",  # M4 L28: bulk rating, a fresh admin session's
    "POST /api/v1/bulk/rate/{order_id}/apply",
    "POST /api/v1/editions/{edition_id}/team-switch",  # M4 L35: an admin's, replaces served files
    "POST /api/v1/editions/{edition_id}/team-switch/{switch_id}/apply",
    "GET /api/v1/editions/{edition_id}/team-switch/{switch_id}",
    "POST /api/v1/imports/tachibk",  # M4 L26: a backup of another reader app, an admin's
    "GET /api/v1/imports/tachibk/{import_id}",
    "POST /api/v1/imports/tachibk/{import_id}/apply",
    "GET /api/v1/request-lists",  # M4 L31: an adult's lists; a child makes and reads none
    "POST /api/v1/request-lists",
    "GET /api/v1/request-lists/{import_id}",
    "POST /api/v1/request-lists/for-child/{user_id}",  # reserved: reading_shelf.attach
    "POST /api/v1/requests/{request_id}/elsewhere",  # M4 L30: put aside, an admin's (EF-USR-019)
    "POST /api/v1/requests/{request_id}/proposals",
    "GET /api/v1/requests/{request_id}/proposals",
    "POST /api/v1/requests/{request_id}/proposals/{proposal_id}/accept",
    "GET /api/v1/admin/sessions",  # M3 L07: every profile's sessions are the admin's
    "DELETE /api/v1/admin/sessions/{session_id}",
    "GET /api/v1/admin/recovery-kit",
)

# The one route whose body is read before its guard answers: it is given a valid one, so that the
# refusal under test is the child's, not a validation error.
BODIES = {
    "POST /api/v1/reconcile/forget-root": {"root_id": "x", "confirm": "x"},
    "POST /api/v1/destinations/{destination_id}/removal": {"mode": "keep_files", "typed": "x"},
    "POST /api/v1/destinations/{destination_id}/removal-preview": {"mode": "keep_files"},
    "POST /api/v1/targets/{target_id}/detach": {"typed": "x"},
    "POST /api/v1/works/{work_id}/relations": {"to_work_id": ULID, "kind": "sequel_of"},
    "POST /api/v1/editions/{edition_id}/source-migrations": {"source_key": "x:1", "remote_id": "r"},
    "POST /api/v1/editions/{edition_id}/source-migrations/{migration_id}/validation": {
        "digest": "d" * 64,
        "overrides": [],
    },
    "POST /api/v1/editions/{edition_id}/source-migrations/{migration_id}/application": {
        "digest": "d" * 64
    },
    "POST /api/v1/request-lists": {"text": "A title"},
    "POST /api/v1/request-lists/for-child/{user_id}": {"text": "A title"},
    "POST /api/v1/ops/blocklist": {"source_key": "s", "remote_unit_id": "x", "reason": "other"},
    "POST /api/v1/webhooks": {"url": "https://hooks.example.org/x", "event_types": ["unit.stored"]},
}

# Surfaces that do not exist yet: the lot that builds each one adds its child case, and this
# list is where the suite notices that the surface has arrived (the meta-test fails when a path
# with that word shows up in the OpenAPI document).
PENDING_SURFACES: dict[
    str, tuple[str, tuple[str, ...]]
] = {}  # none: covers (L24) and OPDS (L48) are in

SURFACES = (
    "search", "search_pages", "search_key", "federated", "me_state_get", "me_state_put",
    "me_state_list", "me_title", "activity", "notification", "works_since", "sse", "thumbnail",
    "content", "reader", "continue_reading", "work_redirect", "opds", "support_author",
    "collections", "relations", "selections",
    "lite",
)  # fmt: skip
METHODS = ("get", "post", "put", "patch", "delete")
HIDDEN_FROM_CEILING_10 = {*HIDDEN_TITLES, "twelve"}
VISIBLE_FOR_CEILING_10 = {"ok", "adult_shelf", "mixed"}


class World:
    """The catalogue, a child with a ceiling of 10, an adult member and the admin."""

    def __init__(self, env: AuthEnv, seed: Seed, kid: TestClient, kid_id: str) -> None:
        self.env, self.seed, self.kid, self.kid_id = env, seed, kid, kid_id

    @property
    def markers(self) -> list[str]:
        """What must never reach a child: the titles and the ids of every hidden Work."""
        hidden = [self.seed.title_of(k) for k in HIDDEN_FROM_CEILING_10]
        return hidden + [self.seed.works[k] for k in HIDDEN_FROM_CEILING_10] + [
            self.seed.editions[k]
            for k in ("above", "adult_edition", "misfiled", "mixed_new", "reserved")
        ]  # fmt: skip

    def clean(self, text: str) -> None:
        leaked = [m for m in self.markers if m in text]
        assert not leaked, f"a child's answer holds {leaked}"

    def put(self, path: str, body: Any) -> Any:
        return self.kid.put(path, json=body, headers={CSRF_HEADER: self.env.csrf(self.kid)})


FAMILY_SOURCE = "famsrc"
# What the family source answers: the titles of Works the admin rated (above her ceiling or not)
# and one title the catalogue knows nothing of.
REMOTE_RATED = ("above", "twelve", "adult_edition")
REMOTE_PLAIN = "Plain Remote Tale"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    import shelfhold.main as main_module
    from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
    from tests.federated_env import FakeSource, hit

    titles = [{**VISIBLE_TITLES, **HIDDEN_TITLES}[k] for k in REMOTE_RATED]
    source = FakeSource(
        FAMILY_SOURCE, name="Family source",
        pages=[[*(hit(f"r-{k}", t) for k, t in zip(REMOTE_RATED, titles, strict=True)),
                hit("r-plain", REMOTE_PLAIN)]],
    )  # fmt: skip
    registry = PluginRegistry(active_keys=None)
    registry.load_all(
        [PluginCandidate(key=source.key, origin=PluginOrigin.FIRST_PARTY, load=lambda: source)]  # type: ignore[arg-type]
    )
    monkeypatch.setattr(main_module, "start_plugins", lambda *_a, **_k: (registry, []))
    with auth_env(monkeypatch, settings) as env:
        yield env


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, api: AuthEnv) -> Iterator[World]:
    seed = seed_catalogue(api)
    kid = add_profile(api, "kid", "child", "10.0.0.2", max_age_rating=10)
    assert run_cli(monkeypatch, api.settings, "search", "rebuild") == 0
    assert api.login().status_code == 200
    kid_id = str(
        seed.engine.connect().execute(sa.text("SELECT id FROM user WHERE username='kid'")).scalar()
    )
    yield World(api, seed, kid, kid_id)
    seed.engine.dispose()


def _routes(app: Any) -> set[str]:
    """Every route of the application, the ones left out of the OpenAPI document included (a
    stream, a thumbnail or a cover is usually `include_in_schema=False`). A route with no HTTP
    method (a mounted application, a WebSocket) is counted too, as `MOUNT` or `WEBSOCKET`: it
    serves a child as well, and needs its case."""
    found: set[str] = set()
    for context in iter_route_contexts(app.routes):  # routers included lazily are walked too
        if context.path is None:
            continue
        own = context.methods or getattr(context.original_route, "methods", None) or set()
        methods = set(own) - {"HEAD", "OPTIONS"}
        if own:  # a HEAD-only route has only the twin of a GET: nothing more to serve
            found.update(f"{m} {context.path}" for m in methods)
        elif isinstance(context.original_route, WebSocketRoute | APIWebSocketRoute):
            found.add(f"WEBSOCKET {context.path}")
        else:
            found.add(f"MOUNT {context.path}")
    return found


def _shape(response: Any) -> tuple[Any, ...]:
    body = response.json()
    return response.status_code, body.get("type"), body.get("title"), body.get("detail")


def _titles(response: Any) -> list[str]:
    return [hit["title"] for hit in response.json()["items"]]


# -- the meta-test -----------------------------------------------------------------------------


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_every_openapi_route_has_a_child_case(api: AuthEnv) -> None:
    """A route added with no child case fails the suite (the end condition of L09)."""
    declared = [*PUBLIC, *OWN, *OPEN, *CLOSED, *DEVICE_ALL]
    assert len(declared) == len(set(declared)), "a route holds two child cases"
    routes = _routes(api.app)
    assert not routes - set(declared), (
        f"routes with no child case: {sorted(routes - set(declared))}"
    )
    assert not set(declared) - routes, (
        f"cases of routes that are gone: {sorted(set(declared) - routes)}"
    )
    module = sys.modules[__name__]
    for surface in set(OPEN.values()):
        assert surface in SURFACES, surface
    assert hasattr(module, "test_unrated_invisible_to_child_on_all_surfaces")


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_pending_surfaces_get_a_real_child_case_when_they_arrive(api: AuthEnv) -> None:
    """A surface announced here (none today: SSE L12, covers L24, notifications L14 and OPDS L48
    have their case): the day a path of that
    surface shows up in the OpenAPI document, this fails until its child case is written."""
    segments = {
        seg for route in _routes(api.app) for seg in route.split(" ", 1)[1].lower().split("/")
    }
    arrived = {
        name: lot for name, (lot, words) in PENDING_SURFACES.items() if segments & set(words)
    }
    assert not arrived, f"surfaces that arrived without a child case: {arrived}"


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_new_route_without_a_child_case_is_caught(api: AuthEnv) -> None:
    """The meta-test is not decoration: a route that no registry names is reported as missing."""
    known = set([*PUBLIC, *OWN, *OPEN, *CLOSED, *DEVICE_ALL])
    assert _routes(api.app) - known == set()  # today: every route has its case
    assert ({*_routes(api.app), "GET /api/v1/zz-new-surface"} - known) == {
        "GET /api/v1/zz-new-surface"
    }


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_meta_test_counts_mounts_and_websockets() -> None:
    """A route with no HTTP method serves a child as well: it is listed, not skipped."""
    from fastapi import FastAPI
    from starlette.responses import PlainTextResponse

    app = FastAPI()
    app.mount(
        "/zz-static", lambda scope, receive, send: PlainTextResponse("x")(scope, receive, send)
    )
    app.add_api_websocket_route("/zz-socket", lambda websocket: None)
    assert _routes(app) >= {"MOUNT /zz-static", "WEBSOCKET /zz-socket"}


# -- the closed routes -----------------------------------------------------------------------


def _fill(route: str) -> tuple[str, str]:
    method, path = route.split(" ", 1)
    out = path
    while "{" in out:
        start, end = out.index("{"), out.index("}")
        out = out[:start] + ULID + out[end + 1 :]
    return method, out


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_child_is_refused_on_every_closed_route(world: World) -> None:
    """Session and key of a child: 403 on every route that is not hers, whatever the verb."""
    env, failures = world.env, []
    key = (
        local_auth(world.seed.engine, env.settings)
        .create_api_key(name="kidkey", username="kid", read_only=True)
        .value
    )
    for route in CLOSED:
        method, path = _fill(route)
        body: dict[str, Any] = {"json": BODIES.get(route, {})} if method != "GET" else {}
        by_session = world.kid.request(
            method, path, headers={CSRF_HEADER: env.csrf(world.kid)}, **body
        )
        by_key = env.other_client("10.0.0.50").request(
            method, path, headers={"Authorization": f"Bearer {key}"}, **body
        )
        for how, response in (("session", by_session), ("key", by_key)):
            if response.status_code != 403:
                failures.append((route, how, response.status_code))
            if response.status_code < 400:  # never a success, never data
                failures.append((route, how, "success"))
            world.clean(response.text)
    assert not failures, failures


MAINTENANCE_ROUTES = CLOSED[1:9]  # the eight routes of M3 L13, listed first in CLOSED


@pytest.mark.req("EF-USR-006", "EF-INT-018", "EF-INT-017")
def test_child_session_and_key_change_nothing_through_the_maintenance_and_queue_routes(
    world: World,
) -> None:
    """M3 L13: the mode, the window and the control of the queue are the admin's. A child, by her
    session or by her key, gets 403 on each route and nothing moves: the mode stays off, no pause
    is recorded, no job changes and no audit line is written."""
    assert all(
        "/maintenance" in r or "/queue/" in r or r.endswith(("cancel", "priority", "replay"))
        for r in MAINTENANCE_ROUTES
    )
    env, engine = world.env, world.seed.engine
    key = (
        local_auth(engine, env.settings)
        .create_api_key(name="kidkey2", username="kid", read_only=True)
        .value
    )

    def state() -> tuple[object, ...]:
        with engine.connect() as conn:
            return (
                conn.execute(sa.text("SELECT maintenance_since FROM instance")).scalar(),
                conn.execute(
                    sa.text("SELECT count(*) FROM app_setting WHERE key LIKE 'queue.%'")
                ).scalar(),
                conn.execute(sa.text("SELECT count(*) FROM job_view")).scalar(),
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM audit WHERE action LIKE 'maintenance.%' "
                        "OR action LIKE 'queue.%'"
                    )
                ).scalar(),
            )

    before = state()
    for route in MAINTENANCE_ROUTES:
        method, path = _fill(route)
        body: dict[str, Any] = {"json": {"source": "source-a", "priority_class": "new"}}
        by_session = world.kid.request(
            method,
            path,
            headers={CSRF_HEADER: env.csrf(world.kid)},
            **(body if method != "GET" else {}),
        )
        by_key = env.other_client("10.0.0.52").request(
            method,
            path,
            headers={"Authorization": f"Bearer {key}"},
            **(body if method != "GET" else {}),
        )
        assert by_session.status_code == 403, (route, "session", by_session.status_code)
        assert by_key.status_code == 403, (route, "key", by_key.status_code)
        world.clean(by_session.text + by_key.text)
    assert state() == before


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_own_and_public_routes_carry_no_catalogue_data(world: World) -> None:
    for route in (*OWN, *PUBLIC):
        method, path = _fill(route)
        if method != "GET":
            continue
        for client in (world.kid, world.env.other_client("10.0.0.51")):
            world.clean(client.get(path).text)


@pytest.mark.req("EF-USR-006", "ENF-SEC-010", "EF-USR-008")
def test_device_routes_refuse_a_session_and_a_key_and_serve_a_child_token_no_data(
    world: World,
) -> None:
    env = world.env
    key = env.app.state.auth_gate.auth.create_api_key(name="k", scope="admin").value  # type: ignore[attr-defined]
    for route in DEVICE:
        method, path = _fill(route)
        assert method == "GET"
        by_session = world.kid.get(path)  # the child's own session
        by_admin = env.client.get(path)  # and the admin's
        by_key = env.other_client("10.0.0.53").get(path, headers={"Authorization": f"Bearer {key}"})
        assert (by_session.status_code, by_admin.status_code, by_key.status_code) == (401, 401, 401)
        world.clean(by_session.text + by_admin.text + by_key.text)
        token = env.app.state.auth_gate.auth.create_device_token(subject_username="kid")  # type: ignore[attr-defined]
        md5 = hashlib.md5(token.secret.encode(), usedforsecurity=False).hexdigest()
        presented = (  # the two doors of one token: kosync takes the md5, OPDS the secret (Basic)
            {"x-auth-user": token.public_id, "x-auth-key": md5}
            if route in KOSYNC
            else {
                "Authorization": "Basic "
                + base64.b64encode(f"{token.public_id}:{token.secret}".encode()).decode()
            }
        )
        by_token = env.other_client("10.0.0.54").get(path, headers=presented)
        if route in KOSYNC:
            expected = {"authorized": "OK"} if route.endswith("/auth") else {}
            assert by_token.status_code == 200 and by_token.json() == expected
        else:  # a feed answers 200, or the 404 of a filled-in identifier nobody has
            assert by_token.status_code in (200, 404), (route, by_token.status_code)
        world.clean(by_token.text)


# -- one test, every surface -----------------------------------------------------------------


def _surface_search(world: World) -> None:
    response = world.kid.get(SEARCH, params={"q": "Tale", "limit": 200})
    assert response.status_code == 200, response.text
    assert sorted(_titles(response)) == sorted(
        world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10
    )
    world.clean(response.text)
    assert response.json()["total"] == len(VISIBLE_FOR_CEILING_10)  # a total never counts hidden
    nothing = world.kid.get(SEARCH, params={"q": "Zzzzqx"}).json()
    for key in HIDDEN_FROM_CEILING_10:  # a hidden Work is exactly like one that does not exist
        answer = world.kid.get(SEARCH, params={"q": world.seed.title_of(key)})
        assert answer.status_code == 200 and answer.json() == nothing, key
        assert world.kid.get(SEARCH, params={"q": world.seed.works[key]}).json() == nothing
    admin = world.env.client.get(SEARCH, params={"q": "Tale", "limit": 200}).json()
    assert len(admin["items"]) == len(VISIBLE_TITLES) + len(HIDDEN_TITLES)  # the barrier is hers


def _surface_search_pages(world: World) -> None:
    seen: list[str] = []
    for offset in range(0, 20):
        page = world.kid.get(SEARCH, params={"q": "Tale", "limit": 1, "offset": offset}).json()
        assert len(page["items"]) <= 1
        assert page["total"] == len(VISIBLE_FOR_CEILING_10)  # the same total on every page
        seen += [h["title"] for h in page["items"]]
        world.clean(str(page))
    assert sorted(seen) == sorted(world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10)
    for limit, offset in ((200, 0), (200, 2), (200, 3), (1, 10**6), (1, 3)):
        page = world.kid.get(SEARCH, params={"q": "Tale", "limit": limit, "offset": offset})
        world.clean(page.text)
    assert (
        world.kid.get(SEARCH, params={"q": "Tale", "limit": 200, "offset": 3}).json()["items"] == []
    )


def _surface_search_key(world: World) -> None:
    """A key of the child's profile reads as the child, never as whoever created it."""
    key = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="kidkey", username="kid", read_only=True)
        .value
    )
    answer = world.env.other_client("10.0.0.52").get(
        SEARCH, params={"q": "Tale", "limit": 200}, headers={"Authorization": f"Bearer {key}"}
    )
    assert answer.status_code == 200, answer.text
    assert sorted(_titles(answer)) == sorted(world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10)
    world.clean(answer.text)


def _surface_federated(world: World) -> None:
    """A remote result has no rating: the child's door is the family search (L19), a reduced form
    that holds no source that is not marked "family", and by key none at all. A family source that
    names Works the admin rated is the attack: they follow the catalogue (hidden above her
    ceiling), they are never offered as unrated results."""
    import sqlite3

    from tests.integration.test_family_search_review_poc import bind

    database = str(world.env.settings.database_path)
    ask = {"q": "Tale", "scope": "all"}
    answer = world.kid.get(SEARCH, params=ask)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert set(body) == {"items", "local", "state", "next_cursor"}  # no total, no count
    assert body["items"] == [] and body["state"] == "empty"  # hide mode, the default: no source
    assert sorted(h["title"] for h in body["local"]) == sorted(
        world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10
    )
    world.clean(answer.text)
    # the family source: ticked, the profile shows the reduced form, the catalogue knows three
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO source_config (plugin_id, family_safe) VALUES (?, 1)", (FAMILY_SOURCE,)
        )
        connection.execute(
            'UPDATE "user" SET hide_unrated_remote = 0 WHERE id = ?', (world.kid_id,)
        )
        connection.commit()
    finally:
        connection.close()
    for key in REMOTE_RATED:
        bind(database, FAMILY_SOURCE, f"r-{key}", world.seed.works[key], world.seed.editions[key])
    reduced = world.kid.get(SEARCH, params=ask)
    assert reduced.status_code == 200, reduced.text
    assert [i["title"] for i in reduced.json()["items"]] == [REMOTE_PLAIN]  # the rest is rated
    world.clean(reduced.text)
    key = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="kidkey2", username="kid", read_only=True)
        .value
    )
    keyed = world.env.other_client("10.0.0.53").get(
        SEARCH, params=ask, headers={"Authorization": f"Bearer {key}"}
    )
    assert keyed.status_code == 403
    world.clean(keyed.text)


def _gone(world: World, method: str, path_of: Any, body: Any = None) -> None:
    """The same 404 for a Work above the ceiling, unrated, not even rated as a whole, or absent."""
    absent = _call(world, method, path_of(MISSING), body)
    assert absent.status_code == 404
    for key in HIDDEN_FROM_CEILING_10:
        answer = _call(world, method, path_of(world.seed.works[key]), body)
        assert _shape(answer) == _shape(absent), key
        world.clean(answer.text)


def _call(world: World, method: str, path: str, body: Any) -> Any:
    headers = {CSRF_HEADER: world.env.csrf(world.kid)}
    if body is None:
        return world.kid.request(method, path, headers=headers)
    return world.kid.request(method, path, headers=headers, json=body)


def _surface_me_state_get(world: World) -> None:
    _gone(world, "GET", lambda w: f"{ME}/state/{w}")
    assert world.kid.get(f"{ME}/state/{world.seed.works['ok']}").status_code == 200


def _surface_work_redirect(world: World) -> None:
    """M3 L22: a merge makes the old identifier lead to the survivor, and the survivor takes the
    strictest age and the unrated barrier: an identifier that now leads to a Work she may not see
    is exactly an absent one (no 404 that differs, no title, no write)."""
    from datetime import UTC, datetime

    from shelfhold.catalog.public import apply_work_merge, plan_work_merge, separate_works

    works, engine = world.seed.works, world.seed.engine
    with engine.begin() as connection:  # the editions of the two Works would share a key
        connection.execute(
            sa.text("UPDATE edition SET lang = 'de' WHERE work_id = :w"),
            {"w": works["adult_shelf"]},
        )
    merges = []
    for keep, drop in (("above", "adult_shelf"), ("ok", "unrated")):
        with engine.connect() as connection:
            plan = plan_work_merge(connection, works[keep], works[drop])
        assert plan.mergeable, plan.blockers
        with engine.begin() as connection:
            merges.append(apply_work_merge(
                connection, works[keep], works[drop], confirm=plan.fingerprint,
                now=datetime(2026, 11, 8, tzinfo=UTC),
            ).merge_id)  # fmt: skip
    for key in ("above", "adult_shelf", "ok", "unrated"):
        for method, path, body in (
            ("GET", f"{ME}/state/{works[key]}", None),
            ("PUT", f"{ME}/state/{works[key]}", {"followed": True}),
            ("GET", f"{ME}/works/{works[key]}/title", None),
        ):
            absent = _call(world, method, path.replace(works[key], MISSING), body)
            answer = _call(world, method, path, body)
            assert answer.status_code == 404 and _shape(answer) == _shape(absent), (key, method)
            world.clean(answer.text)
    rows = engine.connect().execute(sa.text("SELECT work_id FROM user_state")).all()
    assert rows == []  # nothing was written for a Work she may not see, even by its old identifier
    # after « Séparer »: an age is never lowered by an undo, and a half that the merge left unrated
    # stays unrated, so all four answer as an absent Work until the admin rates them by hand
    for merge_id in reversed(merges):
        with engine.begin() as connection:
            separate_works(connection, merge_id, now=datetime(2026, 11, 9, tzinfo=UTC))
    for key in ("above", "adult_shelf", "ok", "unrated"):
        path = f"{ME}/state/{works[key]}"
        absent = _call(world, "GET", path.replace(works[key], MISSING), None)
        answer = _call(world, "GET", path, None)
        assert answer.status_code == 404 and _shape(answer) == _shape(absent), key
        world.clean(answer.text)


def _surface_me_state_put(world: World) -> None:
    _gone(world, "PUT", lambda w: f"{ME}/state/{w}", {"followed": True, "favorite": True})
    rows = world.seed.engine.connect().execute(sa.text("SELECT work_id FROM user_state")).all()
    assert rows == []  # nothing was written for a Work she may not see
    assert world.put(f"{ME}/state/{world.seed.works['ok']}", {"followed": True}).status_code == 200


def _surface_me_state_list(world: World) -> None:
    now = "2026-11-02 09:00:00"
    with world.seed.engine.begin() as connection:  # a flag set before a Work was re-rated upwards
        for key in HIDDEN_FROM_CEILING_10:
            connection.execute(
                sa.text(
                    "INSERT INTO user_state (user_id, work_id, followed, favorite, stamp) "
                    "VALUES (:u, :w, 1, 1, :t)"
                ),
                {"u": world.kid_id, "w": world.seed.works[key], "t": now},
            )
        connection.execute(
            sa.text(
                "INSERT INTO user_state (user_id, work_id, followed, favorite, stamp) "
                "VALUES (:u, :w, 1, 0, :t)"
            ),
            {"u": world.kid_id, "w": world.seed.works["ok"], "t": now},
        )
    answer = world.kid.get(f"{ME}/state")
    assert [i["work_id"] for i in answer.json()["items"]] == [world.seed.works["ok"]]
    world.clean(answer.text)


def _surface_support_author(world: World) -> None:
    """M4 L23: the links of an Edition she may not see are those of an absent one; the ones she
    may see carry no proposal about a binding (the full attacks: `test_l23_support_author_http`)."""
    from tests.integration.test_l23_support_author_http import _link

    for key in ("above", "adult_edition", "misfiled", "mixed_new"):
        _link(world, world.seed.editions[key], f"https://hidden.example.org/{key}")
    absent = world.kid.get(f"/api/v1/editions/{MISSING}/support-author")
    assert absent.status_code == 404
    for key in ("above", "adult_edition", "misfiled", "mixed_new"):
        answer = world.kid.get(f"/api/v1/editions/{world.seed.editions[key]}/support-author")
        assert _shape(answer) == _shape(absent), key
        assert "hidden.example.org" not in answer.text
        world.clean(answer.text)
    shown = world.kid.get(f"/api/v1/editions/{world.seed.editions['ok']}/support-author")
    assert shown.status_code == 200 and shown.json()["pause_suggestions"] == []


def _surface_collections(world: World) -> None:
    """M4 L18a: the lists she is shown hold only Works she may see (the attacks are in
    `test_m4_l18a_collections`)."""
    from tests.integration.test_m4_l18a_collections import child_case

    child_case(world)


def _surface_selections(world: World) -> None:
    """M5 L21: a selection is evaluated in her scope (the attacks are in
    `test_m5_l21_saved_selections`)."""
    from tests.integration.test_m5_l21_saved_selections import child_case

    child_case(world)


def _surface_relations(world: World) -> None:
    """M4 L19: a relation to a Work she may not see is not shown, nor counted (the attacks are in
    `test_m4_l19_relations`)."""
    from tests.integration.test_m4_l19_relations import child_case

    child_case(world)


def _surface_me_title(world: World) -> None:
    _gone(world, "GET", lambda w: f"{ME}/works/{w}/title")
    assert world.kid.get(f"{ME}/works/{world.seed.works['ok']}/title").status_code == 200


def _surface_activity(world: World) -> None:
    from datetime import UTC, datetime

    with world.seed.engine.begin() as connection:
        for _key, work_id in world.seed.works.items():
            connection.execute(
                sa.insert(platform.event).values(
                    type="work.updated", schema_version=1, payload={"work_id": work_id},
                    occurred_at=datetime(2026, 11, 3, 9, 0, tzinfo=UTC), actor_id=world.kid_id,
                    visibility="household",
                )
            )  # fmt: skip
    answer = world.kid.get("/api/v1/activity", params={"limit": 100})
    assert answer.status_code == 200
    world.clean(answer.text)
    shown = {i["payload"]["work_id"] for i in answer.json()}
    assert shown == {world.seed.works[k] for k in VISIBLE_FOR_CEILING_10}


def _filters_and_covers_of_the_list(world: World) -> None:
    """M3 L43: a filter by shelf or by series only adds a condition to the one filter; the series
    choices leave out a series with a hidden member (and its title); a cover address is given only
    for an Edition she may see. Nothing hidden comes back through a filter, a count or a facet."""
    from shelfhold.infrastructure.schema import catalog as catalog_schema
    from tests.thumbnail_env import put_cover

    seed, works = world.seed, world.seed.works
    mixed, hidden_only = new_ulid(), new_ulid()
    with seed.engine.begin() as connection:
        for series_id, title, members in (
            (mixed, "Mixed Saga L43", ("ok", "above")),
            (hidden_only, "Hidden Saga L43", ("above", "x")),
        ):
            connection.execute(
                sa.insert(catalog_schema.series).values(
                    id=series_id, title=title, sort_title=title.lower()
                )
            )
            for position, key in enumerate(members, start=1):
                connection.execute(
                    sa.insert(catalog_schema.series_member).values(
                        series_id=series_id, work_id=works[key], position=str(position)
                    )
                )
        covers = {
            key: put_cover(
                world.env.settings.data_dir, connection, seed.editions[key], (i * 70 + 20, 7, 7)
            )
            for i, key in enumerate(("ok", "above", "mixed_new"))
        }
    queries: list[dict[str, Any]] = [
        {"facets": "true"}, {"series_id": mixed}, {"series_id": hidden_only},
        {"shelf_id": seed.shelves["family"]}, {"shelf_id": seed.shelves["adult"]},
        {"shelf_id": seed.shelves["adult"], "series_id": mixed},
    ]  # fmt: skip
    for query in queries:
        got = world.kid.get("/api/v1/works", params={"limit": 200, **query})
        assert got.status_code == 200, query
        world.clean(got.text)
        assert "Saga L43" not in got.text  # a series with a hidden member: no title
        for hidden in (covers["above"], covers["mixed_new"]):
            assert hidden not in got.text, query  # no address for a cover she may not see
        ids = {i["work_id"] for i in got.json()["items"]}
        assert ids <= seed.visible_ids | {works["twelve"]}, query
        assert got.json()["total"] == len(ids), query
    assert world.kid.get("/api/v1/works", params={"series_id": hidden_only}).json()["items"] == []
    facets = world.kid.get("/api/v1/works", params={"facets": "true"}).json()["series"]
    assert [s["title"] for s in facets] == []  # both series hold a hidden member
    shown = world.kid.get("/api/v1/works", params={"limit": 200}).json()["items"]
    assert {i["cover_hash"] for i in shown if i["cover_hash"]} == {covers["ok"]}


def _surface_works_since(world: World) -> None:
    """The list of Works and its removals are the child's own view: a page of visible Works, and
    a removal only for a Work she saw at the token. By session and by key, never the creator's."""
    first = world.kid.get("/api/v1/works", params={"limit": 200})
    assert first.status_code == 200
    world.clean(first.text)
    assert {i["title"] for i in first.json()["items"]} == {
        world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10
    }
    assert first.json()["total"] == len(VISIBLE_FOR_CEILING_10)
    assert world.kid.get("/api/v1/works", params={"limit": 200, "offset": 3}).json()["items"] == []
    # M3 L41, "Arrivé récemment": the newest-first order is the same view, never a wider one
    recent = world.kid.get("/api/v1/works", params={"limit": 200, "sort": "recent"})
    assert recent.status_code == 200
    world.clean(recent.text)
    assert {i["title"] for i in recent.json()["items"]} == {
        world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10
    }
    assert recent.json()["total"] == len(VISIBLE_FOR_CEILING_10)
    _filters_and_covers_of_the_list(world)
    key = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="kidkey2", username="kid", read_only=True)
        .value
    )
    by_key = world.env.other_client("10.0.0.57").get(
        "/api/v1/works", params={"limit": 200}, headers={"Authorization": f"Bearer {key}"}
    )
    assert by_key.status_code == 200
    world.clean(by_key.text)
    assert {i["work_id"] for i in by_key.json()["items"]} == {
        i["work_id"] for i in first.json()["items"]
    }
    from shelfhold.catalog.application import changes

    token = first.json()["since"]
    with world.seed.engine.begin() as connection:  # hidden Works leave: she never saw them
        for key_name in ("above", "x", "unrated", "flagged"):
            changes.record_work_removed(connection, world.seed.works[key_name])
    answer = world.kid.get("/api/v1/works", params={"since": token, "limit": 200})
    assert answer.status_code == 200
    assert answer.json()["removed"] == []
    world.clean(answer.text)


def _surface_sse(world: World) -> None:
    """The stream is a projection like any other: the rows are the child's own household entries
    about every Work of the catalogue, and what the stream sends is what the filter lets through
    (the attacks on the stream itself are in `test_l12_events_sse.py`)."""
    from datetime import timedelta

    from tests.sse_env import cookie_header, open_stream

    def write() -> None:
        with world.seed.engine.begin() as connection:
            for work_id in world.seed.works.values():
                connection.execute(
                    sa.insert(platform.event).values(
                        type="work.updated", schema_version=1, payload={"work_id": work_id},
                        occurred_at=world.env.clock.now() - timedelta(seconds=1),
                        actor_id=world.kid_id, visibility="household",
                    )
                )  # fmt: skip

    with pytest.MonkeyPatch.context() as patch:
        received = open_stream(
            world.env.app, world.env.clock, patch, cookie=cookie_header(world.kid),
            script=[write], until=lambda r: len(r.heartbeats()) >= 1,
        )  # fmt: skip
    assert received.status == 200
    world.clean("\n".join(received.frames))
    shown = {event["payload"]["work_id"] for _, event in received.events()}
    assert shown == {world.seed.works[k] for k in VISIBLE_FOR_CEILING_10}


def _surface_thumbnail(world: World) -> None:
    """L24: the thumbnail of a cover is a projection like any other. The route asks the same
    filter whether an Edition she may see points at the cover: hidden, absent and unreferenced
    are one 404, by session; her key is refused; nothing is made for what is not hers."""
    from tests.thumbnail_env import put_cover, thumbnail_files

    data, seed = world.env.settings.data_dir, world.seed
    digests: dict[str, str] = {}
    with seed.engine.begin() as connection:
        for index, key in enumerate(
            ("ok", "mixed_rated", *("above", "adult_edition", "mixed_new"))
        ):
            digests[key] = put_cover(data, connection, seed.editions[key], (index * 40, 9, 9))
    absent = world.kid.get(f"/api/v1/thumbnails/{'cd' * 32}/160")
    assert absent.status_code == 404
    for key, digest in digests.items():
        got = world.kid.get(f"/api/v1/thumbnails/{digest}/160")
        world.clean(got.text)
        if key in ("ok", "mixed_rated"):
            assert got.status_code == 200
        else:
            assert _shape(got) == _shape(absent), key
    assert len(thumbnail_files(data)) == 2  # only the two covers she may see were ever made
    key = (
        local_auth(seed.engine, world.env.settings)
        .create_api_key(name="kidkey3", username="kid", read_only=True)
        .value
    )
    by_key = world.env.other_client("10.0.0.58").get(
        f"/api/v1/thumbnails/{digests['ok']}/160", headers={"Authorization": f"Bearer {key}"}
    )
    assert by_key.status_code == 403
    world.clean(by_key.text)


def _surface_content(world: World) -> None:
    """M5 L05a: the bytes of a book are a projection like any other. A ticket is not issued for a
    carrier she may not see; the ticket of an adult, shown by her, is an absent thing; a carrier
    reclassified under her own ticket is an absent thing at the next byte; and every refusal has
    the shape of the answer for a carrier that does not exist."""
    from shelfhold.reading.public import UnknownCarrierError
    from tests.content_env import PNG, FakeArchives, add_carrier, session_id_of, user_id_of

    seed = world.seed
    fake = FakeArchives()
    access = world.env.app.state.content_access  # type: ignore[attr-defined]
    access.archives = fake
    admin_id, kid_id = user_id_of(seed.engine, "admin"), user_id_of(seed.engine, "kid")
    admin_session, kid_session = (
        session_id_of(seed.engine, "admin"),
        session_id_of(seed.engine, "kid"),
    )
    carriers: dict[str, str] = {}
    for key in (
        "ok",
        "mixed_rated",
        "twelve",
        "above",
        "adult_edition",
        "misfiled",
        "mixed_new",
        "reserved",
    ):
        carriers[key] = add_carrier(seed.engine, seed.editions[key], format="epub")
        fake.by_content[carriers[key]] = {"p1.png": PNG}
    absent = world.kid.get("/content/" + "A" * 80 + "/p1.png")
    assert absent.status_code == 404
    for key, carrier in carriers.items():
        adult = access.issue(
            user_id=admin_id, session_id=admin_session, carrier_type="content", carrier_id=carrier
        ).token
        shown = world.kid.get(f"/content/{adult}/p1.png")  # her client, an adult's ticket
        assert _shape(shown) == _shape(absent), key
        world.clean(shown.text)
        if key in ("ok", "mixed_rated"):
            hers = access.issue(
                user_id=kid_id, session_id=kid_session, carrier_type="content", carrier_id=carrier
            ).token
            assert world.kid.get(f"/content/{hers}/p1.png").status_code == 200, key
        else:
            with pytest.raises(UnknownCarrierError):
                access.issue(
                    user_id=kid_id,
                    session_id=kid_session,
                    carrier_type="content",
                    carrier_id=carrier,
                )
    key = (
        local_auth(seed.engine, world.env.settings)
        .create_api_key(name="kidkey4", username="kid", read_only=True)
        .value
    )
    ticket = access.issue(
        user_id=kid_id, session_id=kid_session, carrier_type="content", carrier_id=carriers["ok"]
    ).token
    by_key = world.env.other_client("10.0.0.59").get(
        f"/content/{ticket}/p1.png", headers={"Authorization": f"Bearer {key}"}
    )
    assert by_key.status_code in {401, 403}
    world.clean(by_key.text)


def _surface_reader(world: World) -> None:
    """M5 L06: the manifest of a book is a projection like any other. By the content, by the Unit
    and by an `export_item`, a carrier she may not see answers exactly like one that does not
    exist (no title, no number, no link, no ticket), and the ones she may see are a manifest."""
    import sqlalchemy as sa

    from shelfhold.infrastructure.schema import export, storage
    from tests.content_env import PNG, FakeArchives, add_carrier

    seed = world.seed
    fake = FakeArchives()
    world.env.app.state.content_access.archives = fake  # type: ignore[attr-defined]
    absent = world.kid.get("/api/reader/content/01HZZZZZZZZZZZZZZZZZZZZZZZ/publication")
    assert absent.status_code == 404
    gone = "/api/reader/content/01HZZZZZZZZZZZZZZZZZZZZZZZ"
    absent_where = (world.kid.get(f"{gone}/position"), world.kid.get(f"{gone}/positions"))
    assert [a.status_code for a in absent_where] == [404, 404]
    for key in (
        "ok", "mixed_rated", "twelve", "above", "adult_edition", "misfiled", "mixed_new",
        "reserved",
    ):  # fmt: skip
        carrier = add_carrier(seed.engine, seed.editions[key], format="cbz")
        fake.by_content[carrier] = {"p1.png": PNG}
        with seed.engine.begin() as connection:
            unit = connection.execute(
                sa.select(storage.content.c.unit_id).where(storage.content.c.id == carrier)
            ).scalar_one()
            item = new_ulid()
            connection.execute(
                sa.insert(export.export_item).values(
                    id=item, edition_id=seed.editions[key], kind="unit", content_id=carrier
                )
            )
        fake.item_content[item] = carrier
        for carrier_type, carrier_id in (
            ("content", carrier),
            ("unit", unit),
            ("export_item", item),
        ):
            got = world.kid.get(f"/api/reader/{carrier_type}/{carrier_id}/publication")
            if key in ("ok", "mixed_rated") and carrier_type != "export_item":
                assert got.status_code == 200, (key, carrier_type)
                assert got.json()["pages"], key
            elif key in ("ok", "mixed_rated"):
                # an exported file is a sample only, and one page has nothing to cut from
                assert got.status_code == 409 and got.json()["code"] == "excerpt_unavailable", key
            else:
                assert _shape(got) == _shape(absent), (key, carrier_type)
                assert "/content/" not in got.text
                if carrier_type == "content":  # M5 L07: nor her place, nor her history
                    where = (
                        world.kid.get(f"/api/reader/content/{carrier_id}/position"),
                        world.kid.get(f"/api/reader/content/{carrier_id}/positions"),
                    )
                    assert [_shape(w) for w in where] == [_shape(a) for a in absent_where], key
            world.clean(got.text)
    # M6 L18: the marks of an Edition and the undo of one: a hidden Edition answers like one
    # with no mark, and the undo of a Unit that is not hers is the answer for nothing to undo
    marks = "/api/reader/read-marks"
    for edition_id in (seed.editions["ok"], seed.editions["twelve"], seed.editions["above"], ULID):
        got = world.kid.get(marks, params={"edition_id": edition_id})
        assert got.status_code == 200 and got.json()["items"] == [], edition_id
        world.clean(got.text)
    undone = world.kid.post(
        f"{marks}/{ULID}/undo", headers={CSRF_HEADER: world.env.csrf(world.kid)}
    )
    assert undone.status_code == 200 and undone.json() == {"undone": 0}
    world.clean(undone.text)


def _surface_continue_reading(world: World) -> None:
    """M5 L14: the home of the child lists what she is reading and may see, and nothing of what a
    pull reports on a Work that is hidden from her: the answer is the one of a profile with none."""
    import sqlalchemy as sa

    from shelfhold.infrastructure.schema import export, storage
    from shelfhold.reading.application.progress import insert_position
    from tests.content_env import add_carrier

    home, seed = "/api/reader/continue", world.seed
    nothing = world.kid.get(home)  # before anything is pulled: the answer of an empty history
    assert nothing.status_code == 200 and nothing.json()["items"] == []
    for key in ("ok", "twelve", "above", "adult_edition", "misfiled", "reserved", "mixed_new"):
        carrier = add_carrier(seed.engine, seed.editions[key], format="cbz")
        with seed.engine.begin() as connection:
            unit = connection.execute(
                sa.select(storage.content.c.unit_id).where(storage.content.c.id == carrier)
            ).scalar_one()
            connection.execute(
                sa.update(storage.content).where(storage.content.c.id == carrier)
                .values(content_hash="sha256:" + "c" * 64, content_hash_by="norm@1")
            )  # fmt: skip
            connection.execute(
                sa.insert(export.unit_coverage).values(
                    carrier_type="content", carrier_id=carrier, unit_id=unit, position=1,
                    origin="manual",
                )
            )  # fmt: skip
            insert_position(
                connection, user_id=world.kid_id, carrier_type="content", carrier_id=carrier,
                content_hash="sha256:" + "c" * 64, locator={"page": 3}, percent=0.1,
                client_ts=world.env.clock.now(), device_id="server:kavita", source="server",
                now=world.env.clock.now(),
            )  # fmt: skip
        got = world.kid.get(home)
        assert got.status_code == 200, key
        shown = {i["work_id"] for i in got.json()["items"]}
        assert shown <= {seed.works[k] for k in VISIBLE_FOR_CEILING_10}, key
        for hidden in (*HIDDEN_FROM_CEILING_10, "x", "unrated"):
            assert seed.title_of(hidden) not in got.text, (key, hidden)
        world.clean(got.text)
    final = {i["work_id"] for i in world.kid.get(home).json()["items"]}
    assert final == {seed.works["ok"]}  # the control: what is hers is there


def _surface_opds(world: World) -> None:
    """L48: the OPDS feed behind a device token is a projection like any other. Every feed of the
    graph, every page and every search holds only what her ceiling lets through, and a hidden
    identifier is one 404 with an absent one (the attacks: `tests/integration/test_l48_opds.py`)."""
    from xml.etree import ElementTree as ET

    token = world.env.app.state.auth_gate.auth.create_device_token(subject_username="kid")  # type: ignore[attr-defined]
    client = world.env.other_client("10.0.0.61")
    headers = {
        "Authorization": "Basic "
        + base64.b64encode(f"{token.public_id}:{token.secret}".encode()).decode()
    }
    atom = "{http://www.w3.org/2005/Atom}"
    seen_titles: set[str] = set()
    todo, done = ["/opds"], set()
    while todo:
        path = todo.pop()
        if path in done:
            continue
        done.add(path)
        got = client.get(path, headers=headers)
        assert got.status_code == 200, (path, got.status_code)
        world.clean(got.text)
        root = ET.fromstring(got.content)  # noqa: S314
        seen_titles |= {e.findtext(f"{atom}title") or "" for e in root.iter(f"{atom}entry")}
        todo += [
            x.attrib["href"]
            for x in root.iter(f"{atom}link")
            if x.attrib.get("type", "").startswith("application/atom+xml")
            and "{" not in x.attrib["href"]
        ]
    for key in VISIBLE_FOR_CEILING_10:
        assert world.seed.title_of(key) in seen_titles, key
    for key in HIDDEN_FROM_CEILING_10:
        assert world.seed.title_of(key) not in seen_titles, key
        absent = client.get(f"/opds/works/{ULID}", headers=headers)
        hidden = client.get(f"/opds/works/{world.seed.works[key]}", headers=headers)
        assert _shape(hidden) == _shape(absent), key
        found = client.get("/opds/search", params={"q": world.seed.title_of(key)}, headers=headers)
        assert "<entry" not in found.text  # the query is echoed encoded in a link, never as a hit
    paged = client.get("/opds/works", params={"limit": 1, "offset": 3}, headers=headers)
    assert paged.status_code == 200 and "<entry" not in paged.text  # the visible ones are three
    _opds_pse(world, client, headers)
    _opds2(world, client, headers)


def _opds2(world: World, client: Any, headers: dict[str, str]) -> None:
    """M5 L19: the JSON feed is the same projection: the whole graph holds only what her ceiling
    lets through, and a hidden identifier is one 404, the one of the 1.2 feed."""
    import json

    seen_titles: set[str] = set()
    todo, done = ["/opds/v2"], set()
    while todo:
        path = todo.pop()
        if path in done:
            continue
        done.add(path)
        got = client.get(path, headers=headers)
        assert got.status_code == 200, (path, got.status_code)
        world.clean(got.text)
        feed = json.loads(got.content)
        seen_titles |= {p["metadata"]["title"] for p in feed.get("publications", [])}
        seen_titles |= {n["title"] for n in feed.get("navigation", [])}
        found = list(feed.get("links", [])) + feed.get("navigation", [])
        for pub in feed.get("publications", []):
            found += pub.get("links", [])
        todo += [
            x["href"]
            for x in found
            if x.get("type", "").startswith("application/opds+json") and not x.get("templated")
        ]
    for key in VISIBLE_FOR_CEILING_10:
        assert world.seed.title_of(key) in seen_titles, key
    for key in HIDDEN_FROM_CEILING_10:
        assert world.seed.title_of(key) not in seen_titles, key
        absent = client.get(f"/opds/v2/works/{ULID}", headers=headers)
        hidden = client.get(f"/opds/v2/works/{world.seed.works[key]}", headers=headers)
        assert _shape(hidden) == _shape(absent), key
        found_hit = client.get(
            "/opds/v2/search", params={"q": world.seed.title_of(key)}, headers=headers
        )
        assert json.loads(found_hit.content).get("publications", []) == []


def _opds_pse(world: World, client: Any, headers: dict[str, str]) -> None:
    """M5 L18: the page of an exported volume is a projection like any other. A volume of every
    Edition of the seed is exported; the child's feed offers a stream for the ones she sees and no
    other, and a page of a hidden one answers exactly like a page of an item that does not exist."""
    import sqlalchemy as sa

    from shelfhold.infrastructure.schema import export
    from tests.content_env import PNG, FakeArchives, add_carrier
    from tests.integration.test_schema_storage_export import add_destination, add_target

    seed = world.seed
    fake = FakeArchives()
    world.env.app.state.content_access.archives = fake  # type: ignore[attr-defined]
    items: dict[str, str] = {}
    for key, edition in seed.editions.items():
        carrier = add_carrier(seed.engine, edition, format="cbz")
        fake.by_content[carrier] = {"p1.png": PNG, "p2.png": PNG + b"2"}
        item = new_ulid()
        with seed.engine.begin() as connection:
            connection.execute(
                sa.insert(export.export_item).values(
                    id=item, edition_id=edition, kind="unit", content_id=carrier
                )
            )
        fake.item_content[item] = carrier
        items[key] = item
        with seed.engine.begin() as connection:  # a stream is only offered for a live export
            destination = add_destination(connection, seed.shelves["adult"], add_target(connection))
            connection.execute(
                sa.insert(export.export_record).values(
                    shelf_destination_id=destination, export_item_id=item,
                    locator_type="none", state="written",
                )
            )  # fmt: skip
    absent = client.get(f"/opds/pse/{ULID}/0", headers=headers)
    assert absent.status_code == 404
    offered = ""
    for work_id in seed.works.values():
        got = client.get(f"/opds/works/{work_id}", headers=headers)
        if got.status_code == 200:
            offered += got.text
    for key in ("ok", "mixed_rated"):
        assert items[key] in offered, key
        assert client.get(f"/opds/pse/{items[key]}/0", headers=headers).status_code == 200, key
    for key in ("twelve", "above", "adult_edition", "misfiled", "mixed_new", "reserved", "x"):
        assert items[key] not in offered, key
        page = client.get(f"/opds/pse/{items[key]}/0", headers=headers)
        assert _shape(page) == _shape(absent), key
        world.clean(page.text)


def _surface_lite(world: World) -> None:
    """L17a: `/lite` is a projection like any other. Every page of the graph reached by links,
    every search, and a hidden identifier is one 404 with an absent one (the attacks:
    `tests/integration/test_m5_l17a_lite.py`)."""
    from html.parser import HTMLParser

    class Hrefs(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.found: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.found += [v for k, v in attrs if k == "href" and v]

    token = world.env.app.state.auth_gate.auth.create_device_token(subject_username="kid")  # type: ignore[attr-defined]
    client = world.env.other_client("10.0.0.62")
    form = client.get("/lite/sign-in")
    csrf = form.text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    done = client.post(
        "/lite/sign-in",
        data={"identifier": token.public_id, "secret": token.secret, "csrf": csrf},
        follow_redirects=False,
    )
    assert done.status_code == 303
    seen_text, todo, done_paths = "", ["/lite"], set()
    while todo:
        path = todo.pop()
        if path in done_paths:
            continue
        done_paths.add(path)
        got = client.get(path)
        assert got.status_code == 200, (path, got.status_code)
        world.clean(got.text)
        seen_text += got.text
        parser = Hrefs()
        parser.feed(got.text)
        todo += [h for h in parser.found if h.startswith("/lite") and "/complete" not in h]
    for key in VISIBLE_FOR_CEILING_10:
        assert world.seed.title_of(key) in seen_text, key
    for key in HIDDEN_FROM_CEILING_10:
        assert world.seed.title_of(key) not in seen_text, key
        absent = client.get(f"/lite/works/{ULID}")
        hidden = client.get(f"/lite/works/{world.seed.works[key]}")
        assert (hidden.status_code, hidden.text) == (absent.status_code, absent.text), key
        found = client.get("/lite/search", params={"q": world.seed.title_of(key)})
        # the query is echoed in the box she typed it in, never as a hit
        assert "/lite/works/" not in found.text, key


def _surface_notification(world: World) -> None:
    """A notification (and, later, an SSE frame) is a projection like any other: the one function
    that builds it asks the same filter whether the Work it is about is hers."""
    kid_scope = reader_scope(world.seed.engine, world.kid_id)
    viewer = Viewer(world.kid_id, "child")
    with world.seed.engine.connect() as connection:
        for key, work_id in world.seed.works.items():
            event = ActivityEvent(
                1, "work.updated", "household", {"work_id": work_id}, owner_id=world.kid_id,
                owner_role="child", title=world.seed.title_of(key),
            )  # fmt: skip
            item = project_event(connection, viewer, kid_scope, event)
            assert (item is not None) == (key in VISIBLE_FOR_CEILING_10), key
            if item is not None:
                assert item.title == world.seed.title_of(key)
        unlisted = ActivityEvent(
            2, "edition.filed", "household", {"edition_id": world.seed.editions["mixed_new"]},
            owner_id=world.kid_id, owner_role="child", title="Unrated edition of a rated Work",
        )  # fmt: skip
        assert project_event(connection, viewer, kid_scope, unlisted) is None
        # a payload that names a Work and a hidden one beside it is hidden as a whole
        both = ActivityEvent(
            3, "work.merged", "household",
            {"work_id": world.seed.works["ok"], "other_work_id": world.seed.works["unrated"]},
            owner_id=world.kid_id, owner_role="child", title="x",
        )  # fmt: skip
        assert project_event(connection, viewer, kid_scope, both) is None
        # the adult scope is not narrowed
        admin = project_event(
            connection, Viewer("a", "admin"), scope_of("admin", None),
            ActivityEvent(4, "work.updated", "ops", {"work_id": world.seed.works["unrated"]}),
        )  # fmt: skip
        assert admin is not None
    _notification_built_for_a_child(world)


def _notification_built_for_a_child(world: World) -> None:
    """L14: the real router and sender, in the shape of production: ONE shared channel that
    everybody reads. The child follows every Work of the catalogue, and so does an adult (who
    sees the hidden ones). What leaves on the whole channel, for anyone, names no title, no
    series, no identifier of a hidden Work; the child's own notice counts only what is hers."""
    from shelfhold.events.bus import EventBus
    from shelfhold.infrastructure.schema import catalog, identity
    from shelfhold.integrations.notify import NotificationRouter, NotificationSender
    from tests.auth_env import ManualClock

    engine, clock = world.seed.engine, ManualClock()
    sent: list[tuple[str, str]] = []

    def deliver(user_id: str, message: Any) -> bool:
        sent.append((user_id, f"{message.title}\n{message.body}"))
        return True

    bus = EventBus(clock)
    NotificationRouter(engine, clock).subscribe(bus)
    with engine.begin() as connection:
        adult = connection.execute(
            sa.select(identity.user.c.id).where(identity.user.c.role == "admin")
        ).scalar_one()
        connection.execute(sa.update(identity.user).values(notify_mode="immediate"))
        for who in (world.kid_id, adult):
            for work_id in world.seed.works.values():
                connection.execute(
                    sa.insert(identity.user_state).values(
                        user_id=who, work_id=work_id, followed=True
                    )
                )
    from tests.integration.test_schema_catalog import add_unit

    for work_id in world.seed.works.values():
        with engine.begin() as connection:  # the event names a real Edition and Unit of the Work
            edition_id = connection.execute(
                sa.select(catalog.edition.c.id)
                .where(catalog.edition.c.work_id == work_id)
                .order_by(catalog.edition.c.id)
            ).scalar()
            unit_id = add_unit(connection, edition_id) if edition_id else new_ulid()
            published = bus.publish(
                connection, "unit.stored",
                {"unit_id": unit_id, "edition_id": edition_id or new_ulid(), "work_id": work_id},
            )  # fmt: skip
        bus.deliver([published])
    clock.advance(timedelta(days=1))  # a child is never told at once: the digest, tomorrow 08:00
    NotificationSender(engine, clock, deliver=deliver, pending_withdrawals=lambda _c: 0).run_once()
    channel = "\n".join(t for _u, t in sent)  # everything, whoever it was for
    world.clean(channel)
    for key in (*VISIBLE_FOR_CEILING_10, *HIDDEN_FROM_CEILING_10):
        assert world.seed.title_of(key) not in channel, key
    kid = "\n".join(t for u, t in sent if u == world.kid_id)
    mine = "\n".join(t for u, t in sent if u == adult)
    assert f"{len(VISIBLE_FOR_CEILING_10)} new chapter" in kid  # the control: hers, counted
    assert f"{len(world.seed.works)} new chapter" in mine  # the adult is not narrowed


@pytest.mark.req("EF-USR-006", "ENF-SEC-010", "EF-USR-014")
@pytest.mark.parametrize("surface", SURFACES)
def test_unrated_invisible_to_child_on_all_surfaces(world: World, surface: str) -> None:
    """The acceptance of EF-USR-006: an unrated Work (or one above the ceiling) appears nowhere
    for a child, and nothing she can do tells her that it exists."""
    globals()[f"_surface_{surface}"](world)


# -- the scope is the library of the profile, not the shelf ------------------------------------


@pytest.mark.req("EF-USR-006", "EF-USR-015")
def test_visibility_scope_by_open_shelf(world: World) -> None:
    """ADR-0027 as amended by D-P3 (M4 L39): what a child sees follows the age of the Work against
    her own ceiling **and** the shelves: a Work is hers only if at least one shelf that carries it
    is open to her (`allowed_profiles` empty, or naming her)."""
    seed, ids = world.seed, world.seed.works
    with seed.engine.connect() as connection:
        shown = {k for k, w in ids.items() if work_is_visible(connection, scope_of("child", 10), w)}
    assert shown == VISIBLE_FOR_CEILING_10
    # a rated Work on an adult shelf (open to everyone) is hers
    assert "adult_shelf" in shown
    # an unrated Work misfiled on the family shelf is still not hers: the shelf does not show it
    assert "misfiled" not in shown and "unrated" not in shown
    # a shelf whose `allowed_profiles` names someone else takes the Work away from her ...
    assert "reserved" not in shown
    # ... unless another shelf that carries it is open to her
    assert "ok" in shown
    # a Work on two shelves is one Work: listed and counted once
    with seed.engine.connect() as connection:
        assert count_visible_works(connection, scope_of("child", 10)) == 3
        assert len(list_visible_works(connection, scope_of("child", 10), limit=50, offset=0)) == 3
    # the ceiling is hers: a 13+ child sees the 12+ Work, and a 6+ child sees only the 6+ ones
    with seed.engine.connect() as connection:
        thirteen = {
            w for w in ids.values() if work_is_visible(connection, scope_of("child", 13), w)
        }
        six = {w for w in ids.values() if work_is_visible(connection, scope_of("child", 6), w)}
        zero = {w for w in ids.values() if work_is_visible(connection, scope_of("child", 0), w)}
    assert thirteen == {ids[k] for k in (*VISIBLE_FOR_CEILING_10, "twelve")}
    assert six == {ids[k] for k in VISIBLE_FOR_CEILING_10}
    assert zero == set()  # 0 is the strictest ceiling, never "no ceiling"


@pytest.mark.req("EF-USR-006")
def test_filter_is_applied_in_the_query_not_after(world: World) -> None:
    """Pagination and counts come from the same predicate: a page boundary or a total cannot
    carry a hidden Work out."""
    seed = world.seed
    child = scope_of("child", 10)
    with seed.engine.connect() as connection:
        every = {w for w in seed.works.values()}
        assert visible_work_ids(connection, child, every) == {
            seed.works[k] for k in VISIBLE_FOR_CEILING_10
        }
        assert visible_work_ids(connection, child, {MISSING, ULID}) == set()
        assert visible_work_ids(connection, child, set()) == set()
        walked: list[str] = []
        for offset in range(0, 6):
            walked += [
                h.work_id for h in list_visible_works(connection, child, limit=1, offset=offset)
            ]
        assert sorted(walked) == sorted(seed.works[k] for k in VISIBLE_FOR_CEILING_10)
        assert list_visible_works(connection, child, limit=10, offset=3) == []
        assert list_visible_works(connection, child, limit=-5, offset=0) == []
        assert list_visible_works(connection, child, limit=10, offset=-1) == []
        assert len(list_visible_works(connection, child, limit=10**9, offset=0)) == 3
        assert count_visible_works(connection, child) == 3
        # the adult sees all, the unknown profile sees nothing
        assert count_visible_works(connection, scope_of("member", None)) == len(seed.works)
        assert count_visible_works(connection, scope_of("ghost", None)) == 0
        assert list_visible_works(connection, scope_of("ghost", None), limit=10, offset=0) == []
        assert visible_work_ids(connection, scope_of("ghost", None), every) == set()
        # the Editions: rated, not above, and of a Work she may see
        editions = visible_edition_ids(connection, child, set(seed.editions.values()))
        assert editions == {
            seed.editions[k] for k in ("ok", "ok_second", "adult_shelf", "mixed_rated")
        }
        assert visible_edition_ids(
            connection, scope_of("admin", None), set(seed.editions.values())
        ) == set(seed.editions.values())
        assert (
            visible_edition_ids(connection, scope_of("ghost", None), set(seed.editions.values()))
            == set()
        )


@pytest.mark.req("EF-USR-006")
def test_ceiling_lowered_takes_effect_on_the_next_request(world: World) -> None:
    """The ceiling is read from the database on every request, never kept in the session."""
    first = world.kid.get(SEARCH, params={"q": "Tale", "limit": 200})
    assert len(_titles(first)) == 3
    world.env.app.state.auth_gate.auth.profile_set(world.kid_id, max_age_rating=0)  # type: ignore[attr-defined]
    again = world.kid.get(SEARCH, params={"q": "Tale", "limit": 200})
    assert again.status_code == 200 and _titles(again) == []
    assert world.kid.get(f"{ME}/state/{world.seed.works['ok']}").status_code == 404


@pytest.mark.req("EF-USR-006")
def test_reader_scope_comes_from_the_profile_row(world: World) -> None:
    engine = world.seed.engine
    assert reader_scope(engine, world.kid_id) == scope_of("child", 10, world.kid_id)
    assert reader_scope(engine, MISSING).kind == "none"  # unknown: nothing
    admin_id = str(
        engine.connect().execute(sa.text("SELECT id FROM user WHERE role='admin'")).scalar()
    )
    assert reader_scope(engine, admin_id).unrestricted
    assert reader_scope(engine, None).kind == "none"  # type: ignore[arg-type]


@pytest.mark.req("EF-USR-006")
def test_adults_search_the_whole_catalogue_children_do_not(world: World) -> None:
    mum = add_profile(world.env, "mum", "member", "10.0.0.3")
    assert len(mum.get(SEARCH, params={"q": "Tale", "limit": 200}).json()["items"]) == len(
        VISIBLE_TITLES
    ) + len(HIDDEN_TITLES)
    # `scope=all` stays the administrator's: a child has the family search, a member nothing
    assert mum.get(SEARCH, params={"q": "Tale", "scope": "all"}).status_code == 403
