import { render, screen, within } from "@testing-library/svelte";
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
import Page from "./+page.svelte";

// The webhooks screen (EF-INT-005): list, create, rotate, delete, and the last deliveries of each.
// The address is shown by host only (its path is often a token), the secret of a creation or of a
// rotation is shown exactly once, in the answer and nowhere else, and every control is labelled
// (the a11y contract of the settings screens). Creating, rotating and deleting are the reserved
// action `webhook.manage`: a fresh session, so the screen asks for the password in place and
// replays the same call. Every body is the one the doors take (backend
// `test_m4_l24_webhook_routes.py`, `test_l23a_webhook_rotation.py`, `test_l23b_webhook_deliveries.py`).
const BASE = "http://shelfhold.test";
const server = setupServer();
const ADMIN_PW = ["synthetic", "admin", "pw", "0023"].join("-"); // no literal: gitleaks scans commits
const HOOK_ID = "01JHOOK00000000000000000A";
const OTHER_ID = "01JHOOK00000000000000000B";
const SECRET = ["whsec", "made", "at", "run", "time", "by", "the", "test"].join(
  "_",
);
const OLD_SECRET = ["whsec", "the", "one", "before", "the", "turn"].join("_");
const URL_ONE = "https://hooks.example.org/in/one";

const hook = (over: Record<string, unknown> = {}) => ({
  id: HOOK_ID,
  url_host: "hooks.example.org",
  event_types: ["unit.stored"],
  private_target: null,
  enabled: true,
  created_at: "2026-11-22T12:00:00Z",
  ...over,
});
const delivery = (over: Record<string, unknown> = {}) => ({
  id: "01JDLVR00000000000000000A",
  event_id: 12,
  schema_version: 1,
  status: "delivered",
  attempts: 1,
  last_status: 200,
  last_error: null,
  delivered_at: "2026-11-22T12:01:00Z",
  next_try_at: "2026-11-22T12:01:00Z",
  ...over,
});
const stale = () =>
  HttpResponse.json(
    {
      type: "/api/v1/problems/reauth-required",
      title: "x",
      status: 401,
      code: "reauth_required",
      auth_age_s: 2000,
      max_age_s: 900,
      reauth_methods: ["password"],
    },
    { status: 401 },
  );
const forbidden = () =>
  HttpResponse.json(
    {
      type: "/api/v1/problems/forbidden",
      title: "x",
      status: 403,
      code: "forbidden",
    },
    { status: 403 },
  );
const refused = (code: string) =>
  HttpResponse.json(
    { type: `/api/v1/problems/${code}`, title: "x", status: 400, code },
    { status: 400 },
  );

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = "csrf=tok123";
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const list = (items: unknown[] = [hook()]) =>
  server.use(
    http.get(`${BASE}/api/v1/webhooks`, () => HttpResponse.json({ items })),
  );
const deliveries = (id: string, items: unknown[] = []) =>
  server.use(
    http.get(`${BASE}/api/v1/webhooks/${id}/deliveries`, () =>
      HttpResponse.json({ items }),
    ),
  );
const row = async (host = "hooks.example.org") =>
  (await screen.findByText(host)).closest("li") as HTMLElement;
const reauthConfirm = () =>
  within(
    screen.getByRole("region", { name: "Confirmez votre identité" }),
  ).getByRole("button", {
    name: "Confirmer",
  });
const reauthWith = (counter: { n: number }) =>
  http.post(`${BASE}/api/v1/auth/reauth`, async ({ request }) => {
    counter.n += 1;
    expect(await request.json()).toEqual({
      method: "password",
      password: ADMIN_PW,
    });
    return HttpResponse.json({ auth_time: "now", csrf_token: "tok456" });
  });

describe("Écran des webhooks", () => {
  // req: EF-INT-005
  it("lists the webhooks by host with their types and their last deliveries", async () => {
    list();
    deliveries(HOOK_ID, [
      delivery(),
      delivery({
        id: "01JDLVR00000000000000000B",
        event_id: 13,
        status: "pending",
        attempts: 2,
        last_status: 503,
        last_error: "http_status",
        delivered_at: null,
      }),
    ]);
    render(Page);
    expect(
      await screen.findByRole("heading", { name: "Webhooks", level: 1 }),
    ).toBeTruthy();
    const li = await row();
    expect(li).toHaveTextContent("unit.stored");
    expect(li).not.toHaveTextContent("/in/one"); // the path of the address is never shown
    const table = within(li).getByRole("table", {
      name: "Dernières livraisons",
    });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3); // the header and the two deliveries
    expect(within(rows[1] as HTMLElement).getByText("livrée")).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText("200")).toBeTruthy();
    expect(within(rows[2] as HTMLElement).getByText("en attente")).toBeTruthy();
    expect(within(rows[2] as HTMLElement).getByText("2")).toBeTruthy(); // the attempts of the one that did not arrive
    expect(within(rows[2] as HTMLElement).getByText("503")).toBeTruthy();
    expect(
      within(rows[2] as HTMLElement).getByText("http_status"),
    ).toBeTruthy(); // the closed code of the failure
  });

  // req: EF-INT-005
  it("says a delivery that was given up, and one that is still to be tried", async () => {
    list();
    deliveries(HOOK_ID, [
      delivery({
        status: "dead",
        attempts: 8,
        last_status: null,
        last_error: "timeout",
        delivered_at: null,
      }),
      delivery({
        id: "01JDLVR00000000000000000C",
        event_id: 14,
        status: "pending",
        attempts: 0,
        last_status: null,
        delivered_at: null,
      }),
    ]);
    render(Page);
    const li = await row();
    expect(within(li).getByText("abandonnée")).toBeTruthy();
    expect(within(li).getByText("timeout")).toBeTruthy();
    expect(within(li).getByText("en attente")).toBeTruthy();
  });

  // req: EF-INT-005
  it("creates a webhook and shows its secret exactly once", async () => {
    let created: unknown = null;
    server.use(
      http.get(`${BASE}/api/v1/webhooks`, () =>
        HttpResponse.json({ items: created ? [hook()] : [] }),
      ),
      http.post(`${BASE}/api/v1/webhooks`, async ({ request }) => {
        created = await request.json();
        return HttpResponse.json(
          { ...hook(), secret: SECRET },
          { status: 201 },
        );
      }),
      http.get(`${BASE}/api/v1/webhooks/${HOOK_ID}/deliveries`, () =>
        HttpResponse.json({ items: [] }),
      ),
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Adresse"), URL_ONE);
    await user.type(screen.getByLabelText("Types d’événements"), "unit.stored");
    await user.click(screen.getByRole("button", { name: "Ajouter" }));
    expect(created).toEqual({
      url: URL_ONE,
      event_types: ["unit.stored"],
      allow_private: false,
    });
    expect(await screen.findByText("hooks.example.org")).toBeTruthy();
    expect(screen.getAllByText(SECRET)).toHaveLength(1); // once, and never in the list
    expect(screen.getByText(/ne sera plus affiché/)).toBeTruthy();
  });

  // req: EF-INT-005
  it("sends several event types, trimmed, and the private allowance when it is asked", async () => {
    let created: Record<string, unknown> = {};
    server.use(
      http.get(`${BASE}/api/v1/webhooks`, () =>
        HttpResponse.json({ items: [] }),
      ),
      http.post(`${BASE}/api/v1/webhooks`, async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          { ...hook(), secret: SECRET },
          { status: 201 },
        );
      }),
      http.get(`${BASE}/api/v1/webhooks/${HOOK_ID}/deliveries`, () =>
        HttpResponse.json({ items: [] }),
      ),
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(
      await screen.findByLabelText("Adresse"),
      "http://nas.example.lan:8080/hook",
    );
    await user.type(
      screen.getByLabelText("Types d’événements"),
      " unit.stored , backup.failed ",
    );
    await user.click(screen.getByLabelText("Autoriser une adresse privée"));
    await user.click(screen.getByRole("button", { name: "Ajouter" }));
    expect(created).toEqual({
      url: "http://nas.example.lan:8080/hook",
      event_types: ["unit.stored", "backup.failed"],
      allow_private: true,
    });
  });

  // req: EF-INT-005
  it("says why the server refused the address, and shows no secret", async () => {
    server.use(
      http.get(`${BASE}/api/v1/webhooks`, () =>
        HttpResponse.json({ items: [] }),
      ),
      http.post(`${BASE}/api/v1/webhooks`, () =>
        refused("webhook.url_refused"),
      ),
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(
      await screen.findByLabelText("Adresse"),
      "https://inside.example.org/hook",
    );
    await user.type(screen.getByLabelText("Types d’événements"), "unit.stored");
    await user.click(screen.getByRole("button", { name: "Ajouter" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(
      /adresse a été refusée/,
    );
    expect(screen.queryByText(/ne sera plus affiché/)).toBeNull();
  });

  // req: EF-INT-005
  it("says when the event types were refused", async () => {
    server.use(
      http.get(`${BASE}/api/v1/webhooks`, () =>
        HttpResponse.json({ items: [] }),
      ),
      http.post(`${BASE}/api/v1/webhooks`, () =>
        refused("webhook.event_types"),
      ),
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Adresse"), URL_ONE);
    await user.type(screen.getByLabelText("Types d’événements"), "nope");
    await user.click(screen.getByRole("button", { name: "Ajouter" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(
      /types d’événements ont été refusés/,
    );
  });

  // req: EF-INT-005
  it("rotates the secret and shows the new one exactly once, the old one gone", async () => {
    let rotated = 0;
    list();
    deliveries(HOOK_ID);
    server.use(
      http.post(`${BASE}/api/v1/webhooks/${HOOK_ID}/rotate`, () => {
        rotated += 1;
        return HttpResponse.json({ ...hook(), secret: SECRET });
      }),
    );
    render(Page);
    const user = userEvent.setup();
    await screen.findByText("hooks.example.org");
    await user.click(
      screen.getByRole("button", { name: "Faire tourner le secret" }),
    );
    expect(rotated).toBe(1);
    expect(screen.getAllByText(SECRET)).toHaveLength(1); // once, and never in the list
    expect(screen.getAllByText("hooks.example.org")).toHaveLength(1);
    expect(screen.queryByText(OLD_SECRET)).toBeNull();
    expect(screen.getByText(/l’ancien signe encore 24 heures/)).toBeTruthy();
  });

  // req: EF-INT-005
  it("deletes a webhook and says the list is empty", async () => {
    let items: unknown[] = [hook()];
    let deleted = 0;
    server.use(
      http.get(`${BASE}/api/v1/webhooks`, () => HttpResponse.json({ items })),
      http.delete(`${BASE}/api/v1/webhooks/${HOOK_ID}`, () => {
        deleted += 1;
        items = [];
        return new HttpResponse(null, { status: 204 });
      }),
    );
    deliveries(HOOK_ID);
    render(Page);
    const user = userEvent.setup();
    await screen.findByText("hooks.example.org");
    await user.click(screen.getByRole("button", { name: "Supprimer" }));
    expect(deleted).toBe(0); // asked first, not done
    await user.click(
      await screen.findByRole("button", { name: "Supprimer ce webhook" }),
    );
    await vi.waitFor(() => expect(deleted).toBe(1));
    expect(await screen.findByText("Aucun webhook.")).toBeTruthy();
    expect(screen.queryByText("hooks.example.org")).toBeNull();
  });

  // req: EF-USR-022
  it("asks for the password in place before the creation, then replays it", async () => {
    const bodies: unknown[] = [];
    const reauths = { n: 0 };
    server.use(
      http.get(`${BASE}/api/v1/webhooks`, () =>
        HttpResponse.json({ items: [] }),
      ),
      http.post(`${BASE}/api/v1/webhooks`, async ({ request }) => {
        bodies.push(await request.json());
        return bodies.length === 1
          ? stale()
          : HttpResponse.json({ ...hook(), secret: SECRET }, { status: 201 });
      }),
      http.get(`${BASE}/api/v1/webhooks/${HOOK_ID}/deliveries`, () =>
        HttpResponse.json({ items: [] }),
      ),
      reauthWith(reauths),
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Adresse"), URL_ONE);
    await user.type(screen.getByLabelText("Types d’événements"), "unit.stored");
    await user.click(screen.getByRole("button", { name: "Ajouter" }));
    await user.type(await screen.findByLabelText("Mot de passe"), ADMIN_PW);
    expect(bodies).toHaveLength(1);
    await user.click(reauthConfirm());
    await screen.findByText(/ne sera plus affiché/);
    expect(reauths.n).toBe(1);
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
  });

  // req: EF-USR-022
  it("asks for the password in place before a rotation, then replays it", async () => {
    const reauths = { n: 0 };
    let calls = 0;
    list();
    deliveries(HOOK_ID);
    server.use(
      http.post(`${BASE}/api/v1/webhooks/${HOOK_ID}/rotate`, () => {
        calls += 1;
        return calls === 1
          ? stale()
          : HttpResponse.json({ ...hook(), secret: SECRET });
      }),
      reauthWith(reauths),
    );
    render(Page);
    const user = userEvent.setup();
    await screen.findByText("hooks.example.org");
    await user.click(
      screen.getByRole("button", { name: "Faire tourner le secret" }),
    );
    await user.type(await screen.findByLabelText("Mot de passe"), ADMIN_PW);
    await user.click(reauthConfirm());
    await screen.findByText(/ne sera plus affiché/);
    expect(reauths.n).toBe(1);
    expect(calls).toBe(2);
  });

  // req: EF-USR-001, ENF-SEC-010
  it("shows nothing to a member or a child: the server refused the list, and no action is offered", async () => {
    server.use(http.get(`${BASE}/api/v1/webhooks`, forbidden));
    render(Page);
    expect((await screen.findByRole("alert")).textContent).toMatch(
      /réservés à l'administrateur/,
    );
    expect(screen.queryByRole("button", { name: "Ajouter" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Supprimer" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Faire tourner le secret" }),
    ).toBeNull();
    expect(screen.queryByLabelText("Adresse")).toBeNull();
  });

  // req: EF-INT-005
  it("says so when there is no webhook, and offers nothing to rotate or delete", async () => {
    list([]);
    render(Page);
    expect(await screen.findByText("Aucun webhook.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Supprimer" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Faire tourner le secret" }),
    ).toBeNull();
  });

  // req: EF-INT-005
  it("keeps the list when the deliveries of one webhook cannot be read", async () => {
    list([hook(), hook({ id: OTHER_ID, url_host: "other.example.org" })]);
    server.use(
      http.get(`${BASE}/api/v1/webhooks/${HOOK_ID}/deliveries`, () =>
        HttpResponse.json({}, { status: 500 }),
      ),
      http.get(`${BASE}/api/v1/webhooks/${OTHER_ID}/deliveries`, () =>
        HttpResponse.json({ items: [delivery()] }),
      ),
    );
    render(Page);
    const first = await row();
    expect(
      within(first).getByText("Dernières livraisons indisponibles."),
    ).toBeTruthy();
    const second = await row("other.example.org");
    expect(within(second).getByText("livrée")).toBeTruthy();
  });

  // Fix: remove() should only clear the secret if it's for the same subscription
  // req: EF-INT-005
  it("does not clear the secret when deleting a different subscription", async () => {
    let items: unknown[] = [
      hook(),
      hook({ id: OTHER_ID, url_host: "other.example.org" }),
    ];
    let deletedId: string | null = null;
    server.use(
      http.get(`${BASE}/api/v1/webhooks`, () => HttpResponse.json({ items })),
      http.get(`${BASE}/api/v1/webhooks/${HOOK_ID}/deliveries`, () =>
        HttpResponse.json({ items: [] }),
      ),
      http.get(`${BASE}/api/v1/webhooks/${OTHER_ID}/deliveries`, () =>
        HttpResponse.json({ items: [] }),
      ),
      http.delete(`${BASE}/api/v1/webhooks/${OTHER_ID}`, () => {
        deletedId = OTHER_ID;
        items = [hook()];
        return new HttpResponse(null, { status: 204 });
      }),
    );
    render(Page);
    const user = userEvent.setup();
    await screen.findByText("hooks.example.org");
    // Rotate the first hook to show its secret
    server.use(
      http.post(`${BASE}/api/v1/webhooks/${HOOK_ID}/rotate`, () => {
        return HttpResponse.json({ ...hook(), secret: SECRET });
      }),
    );
    const firstLi = (await screen.findByText("hooks.example.org")).closest(
      "li",
    ) as HTMLElement;
    await user.click(
      within(firstLi).getByRole("button", { name: "Faire tourner le secret" }),
    );
    expect(await screen.findByText(SECRET)).toBeTruthy();
    // Now delete the OTHER hook - the secret should still be shown
    const secondLi = screen
      .getByText("other.example.org")
      .closest("li") as HTMLElement;
    await user.click(
      within(secondLi).getByRole("button", { name: "Supprimer" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Supprimer ce webhook" }),
    );
    await vi.waitFor(() => expect(deletedId).toBe(OTHER_ID));
    // The secret should still be visible
    expect(screen.getByText(SECRET)).toBeTruthy();
    // Only one hook should remain
    expect(screen.getAllByText("hooks.example.org")).toHaveLength(1);
    expect(screen.queryByText("other.example.org")).toBeNull();
  });

  // Fix: distinguish secret_unreadable error
  // req: EF-INT-005
  it("shows a dedicated message when the secret cannot be read during rotation", async () => {
    list();
    deliveries(HOOK_ID);
    server.use(
      http.post(`${BASE}/api/v1/webhooks/${HOOK_ID}/rotate`, () =>
        HttpResponse.json(
          {
            type: "/api/v1/problems/webhook.secret_unreadable",
            title: "x",
            status: 503,
            code: "webhook.secret_unreadable",
          },
          { status: 503 },
        ),
      ),
    );
    render(Page);
    const user = userEvent.setup();
    await screen.findByText("hooks.example.org");
    await user.click(
      screen.getByRole("button", { name: "Faire tourner le secret" }),
    );
    expect((await screen.findByRole("alert")).textContent).toMatch(
      /ne peut plus être lu/,
    );
  });

  // req: EF-INT-005
  it("labels every control and every column of the deliveries (a11y contract)", async () => {
    list();
    deliveries(HOOK_ID, [delivery()]);
    render(Page);
    await screen.findByText("hooks.example.org");
    for (const control of await screen.findAllByRole("button")) {
      expect(control).toHaveAccessibleName();
    }
    expect(screen.getByLabelText("Adresse")).toBeTruthy();
    expect(screen.getByLabelText("Types d’événements")).toBeTruthy();
    expect(screen.getByLabelText("Autoriser une adresse privée")).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "Webhooks", level: 1 }),
    ).toBeTruthy();
    const table = screen.getByRole("table", { name: "Dernières livraisons" });
    for (const name of [
      "Événement",
      "État",
      "Tentatives",
      "Code HTTP",
      "Erreur",
      "Livrée le",
      "Prochaine tentative",
    ]) {
      expect(within(table).getByRole("columnheader", { name })).toBeTruthy();
    }
    expect(within(table).getByRole("cell", { name: "12" })).toBeTruthy(); // the event, named
  });
});
