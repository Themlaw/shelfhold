import { render, screen, waitFor, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { client } from "#lib/api/client.gen.ts";
import { setUiLanguage } from "#lib/i18n/index.ts";
import Page from "./+page.svelte";

vi.mock("$app/state", () => ({ page: { params: { id: "ED1" } } }));

const BASE = "http://shelfhold.test";
const API = `${BASE}/api/v1/editions/ED1`;
const MIGRATIONS = `${API}/source-migrations`;
const server = setupServer();

const row = (
  number: string,
  target: string,
  unit: string | null,
  basis: string,
) => ({
  number,
  place: `main||${number}|`,
  source_order: 0,
  target,
  target_order: 0,
  unit_id: unit,
  basis,
});
const proposal = (over: Record<string, unknown> = {}) => ({
  id: "MIG1",
  edition_id: "ED1",
  state: "proposed",
  digest: "a".repeat(64),
  offset: -10,
  held_count: 2,
  unmatched_units: [],
  to_fetch: 1,
  binding_id: null,
  rows: [
    row("11", "1", "U1", "offset"),
    row("12", "2", "U2", "offset"),
    row("13", "3", null, "new"),
  ],
  ...over,
});
const sheet = {
  edition_id: "ED1",
  work_id: "WK1",
  title: "Une série",
  bindings: [
    {
      id: "OLD",
      source_key: "src-old",
      rank: 1,
      active: false,
      disabled_reason: "removed_upstream",
      last_checked: null,
      next_expected: null,
    },
  ],
};

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = "csrf=tok123";
  setUiLanguage("fr");
  server.use(http.get(`${API}/sheet`, () => HttpResponse.json(sheet)));
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

async function propose(user: ReturnType<typeof userEvent.setup>) {
  await user.type(
    await screen.findByLabelText(/Source de la nouvelle série/),
    "suwayomi:7002",
  );
  await user.type(
    screen.getByLabelText(/Identifiant de la série/),
    "/new-title",
  );
  await user.click(
    screen.getByRole("button", { name: /Proposer une correspondance/ }),
  );
}

describe("Source migration", () => {
  // req: EF-BIB-014
  it("test_source_migration_ui", async () => {
    const user = userEvent.setup();
    const seen: Record<string, unknown>[] = [];
    server.use(
      http.post(MIGRATIONS, async ({ request }) => {
        expect(request.headers.get("x-csrf-token")).toBe("tok123");
        seen.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(proposal(), { status: 201 });
      }),
      http.post(`${MIGRATIONS}/MIG1/validation`, async ({ request }) => {
        seen.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(
          proposal({ state: "validated", digest: "b".repeat(64) }),
        );
      }),
      http.post(`${MIGRATIONS}/MIG1/application`, async ({ request }) => {
        seen.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(
          proposal({
            state: "applied",
            digest: "b".repeat(64),
            binding_id: "NEW",
          }),
        );
      }),
    );
    render(Page);
    await propose(user);

    // the table: every number of the new source, the number it lands on, why
    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 3);
    expect(within(table).getByText("11")).toBeInTheDocument();
    expect(within(table).getAllByText(/décalage/)).toHaveLength(2);
    expect(within(table).getByText(/à télécharger/)).toBeInTheDocument();
    expect(seen[0]).toEqual({
      source_key: "suwayomi:7002",
      remote_id: "/new-title",
      replaces_binding_id: "OLD",
    });
    // nothing can be applied before the table is validated
    expect(
      screen.queryByRole("button", { name: /Appliquer la migration/ }),
    ).toBeNull();
    expect(
      screen.getByText(/rien n'est écrit tant que vous n'avez pas validé/i),
    ).toBeInTheDocument();

    // validating sends the digest of the table that was shown
    await user.click(
      screen.getByRole("button", { name: /Valider ce tableau/ }),
    );
    expect(
      await screen.findByRole("button", { name: /Appliquer la migration/ }),
    ).toBeEnabled();
    expect(seen[1]).toEqual({ digest: "a".repeat(64), overrides: [] });

    // applying sends the digest of the validated table, and says what will be fetched
    await user.click(
      screen.getByRole("button", { name: /Appliquer la migration/ }),
    );
    expect(await screen.findByText(/Migration appliquée/)).toBeInTheDocument();
    expect(seen[2]).toEqual({ digest: "b".repeat(64) });
    expect(screen.getByText(/1 numéro à télécharger/)).toBeInTheDocument();
    expect(screen.getByText(/historique de lecture/i)).toBeInTheDocument();
  });

  // req: EF-BIB-014
  it("test_a_row_the_admin_changes_goes_to_the_server_as_an_override", async () => {
    const user = userEvent.setup();
    const seen: Record<string, unknown>[] = [];
    server.use(
      http.post(MIGRATIONS, () =>
        HttpResponse.json(proposal(), { status: 201 }),
      ),
      http.post(`${MIGRATIONS}/MIG1/validation`, async ({ request }) => {
        seen.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(
          proposal({ state: "validated", digest: "c".repeat(64) }),
        );
      }),
    );
    render(Page);
    await propose(user);
    const field = await screen.findByLabelText(
      /Numéro de la bibliothèque pour 13/,
    );
    await user.clear(field);
    await user.type(field, "9");
    await user.click(
      screen.getByRole("button", { name: /Valider ce tableau/ }),
    );
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0]).toEqual({
      digest: "a".repeat(64),
      overrides: [{ number: "13", target: "9" }],
    });
  });

  // req: EF-BIB-014
  it("test_a_collision_is_shown_and_nothing_is_applied", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(MIGRATIONS, () =>
        HttpResponse.json(proposal(), { status: 201 }),
      ),
      http.post(`${MIGRATIONS}/MIG1/validation`, () =>
        HttpResponse.json(
          {
            type: "/api/v1/problems/source-migration-collision",
            title: "Collision",
            status: 422,
            code: "source_migration.collision",
          },
          {
            status: 422,
            headers: { "content-type": "application/problem+json" },
          },
        ),
      ),
    );
    render(Page);
    await propose(user);
    await user.click(
      await screen.findByRole("button", { name: /Valider ce tableau/ }),
    );
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Appliquer la migration/ }),
    ).toBeNull();
  });
});
