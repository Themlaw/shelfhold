import { render, screen, within } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

const action = (command: string, over: Record<string, unknown> = {}) => ({
  command,
  reserved: false,
  text: `Texte de ${command}`,
  ...over
});
const page = (over: Record<string, unknown> = {}) => ({
  generated_at: '2026-11-13T09:00:00Z',
  sources: [
    { plugin_id: 'good', state: 'ok', code: null, since: null, action: null },
    { plugin_id: 'down', state: 'degraded', code: 'source.broken', since: null, action: action('shelfhold source recheck') }
  ],
  irreplaceable: {
    count: 3,
    without_kept_copy: 1,
    single_copy: 1,
    store_backup: { declared: true, age_days: 4 }
  },
  lost_originals: [
    { code: 'content.original_lost', unit_id: 'U1', content_id: 'C1', action: action('shelfhold content reneutralize') }
  ],
  withdrawal_pending: { count: 2, action: action('shelfhold shelf withdraw', { reserved: true }) },
  kavita_audit: [{ code: 'child_exposure', subject: '01TG', action: action('shelfhold target recheck 01TG') }],
  unreadable_webhooks: [],
  library_type_changes: [
    { code: 'library_type_changed', subject: '01TH', action: action('shelfhold target reconnect 01TH') }
  ],
  page_map_warnings: [{ code: 'relay.page_count_mismatch', subject: '01ED', chapters: 2 }],
  kosync_unknown: [{ code: 'kosync.unknown', token: 'tok-public', count: 3 }],
  jurisdiction: null,
  backup: {
    last_ok_at: '2026-11-12T01:00:00Z',
    age_hours: 32,
    warnings: [{ code: 'backup_stale', action: action('shelfhold backup run') }]
  },
  stats: { human_interventions: { a: 2, b: 1 }, units_exported: 12, arrivals_pending: 0 },
  caches: [
    { kind: 'thumbnails', entries: 12, used_bytes: 1000, quota_bytes: 2000, over: false },
    { kind: 'renditions', entries: 3, used_bytes: 5000, quota_bytes: 4000, over: true },
    { kind: 'http', entries: 7, used_bytes: null, quota_bytes: 3000, over: false },
    { kind: 'metadata', entries: 100, used_bytes: 2000, quota_bytes: 268435456, quota_entries: 4096, over: false }
  ],
  ...over
});
const serve = (body: unknown) => http.get(`${BASE}/api/v1/health/page`, () => HttpResponse.json(body as never));

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Health page', () => {
  // req: ENF-OBS-005
  // req: EF-INT-025
  it('lists every entry with its code and the command that leaves it', async () => {
    server.use(serve(page()));
    render(Page);
    const sources = await screen.findByRole('region', { name: 'Sources' });
    expect(within(sources).getByText('source.broken')).toBeInTheDocument();
    expect(within(sources).getByText('shelfhold source recheck')).toBeInTheDocument();
    expect(screen.getByText('content.original_lost')).toBeInTheDocument();
    expect(screen.getByText('shelfhold content reneutralize')).toBeInTheDocument();
    expect(screen.getByText('backup_stale')).toBeInTheDocument();
    expect(screen.getByText('shelfhold backup run')).toBeInTheDocument();
    expect(screen.getByText('shelfhold target recheck 01TG')).toBeInTheDocument();
    expect(screen.getByText('shelfhold target reconnect 01TH')).toBeInTheDocument();
  });

  // req: EF-BIB-031
  it('says how many Units are irreplaceable, without a kept copy, and the age of the backup', async () => {
    server.use(serve(page()));
    render(Page);
    expect(await screen.findByText(/3 Unités irremplaçables : 1 sans .* conservée, 1 avec une seule/)).toBeInTheDocument();
    expect(screen.getByText(/il y a 4 jours/)).toBeInTheDocument();
  });

  // req: EF-BIB-031
  it('marks a reserved action as reserved and never offers a button to run anything', async () => {
    server.use(serve(page()));
    render(Page);
    expect(await screen.findByText('shelfhold shelf withdraw')).toBeInTheDocument();
    expect(screen.getAllByText(/Action réservée/)).toHaveLength(1);
    expect(screen.queryByRole('button')).toBeNull();
  });

  // req: EF-EXP-036
  it('shows the Kavita audit and the library type changes, or says there are none', async () => {
    server.use(serve(page({ kavita_audit: [], library_type_changes: [] })));
    render(Page);
    expect(await screen.findByText(/Aucun compte Kavita ne voit plus/)).toBeInTheDocument();
    expect(screen.getByText("Aucun type de bibliothèque n'a changé.")).toBeInTheDocument();
  });

  // req: EF-LEC-009
  // req: EF-EXP-026
  it('shows the chapters not relayed to Kavita and the positions an e-reader sent that were not taken', async () => {
    server.use(serve(page()));
    render(Page);
    const region = await screen.findByRole('region', { name: 'Liseuses et relais vers Kavita' });
    expect(within(region).getByText('relay.page_count_mismatch')).toBeInTheDocument();
    expect(within(region).getByText(/2 chapitres d'un bloc ne sont pas relayés vers Kavita/)).toBeInTheDocument();
    expect(within(region).getByText('kosync.unknown')).toBeInTheDocument();
    expect(within(region).getByText(/3 positions envoyées par la liseuse tok-public/)).toBeInTheDocument();
  });

  // req: EF-LEC-009
  it('shows no relay section when no e-reader or chapter has a warning', async () => {
    server.use(serve(page({ page_map_warnings: [], kosync_unknown: [] })));
    render(Page);
    await screen.findByRole('region', { name: 'Sources' });
    expect(screen.queryByRole('region', { name: 'Liseuses et relais vers Kavita' })).toBeNull();
  });

  // req: EF-BIB-018
  it('shows the use and the quota of each cache, and which one is over', async () => {
    server.use(serve(page()));
    render(Page);
    const region = await screen.findByRole('region', { name: 'Caches' });
    expect(within(region).getAllByRole('definition')).toHaveLength(4);
    expect(within(region).getByText(/12 entrées, 1.?000 octets sur un quota de 2.?000/)).toBeInTheDocument();
    expect(within(region).getByText(/3 entrées, 5.?000 octets sur un quota de 4.?000/)).toBeInTheDocument();
    expect(within(region).getByText(/7 entrées \(quota : 3.?000 octets\)/)).toBeInTheDocument();
    expect(within(region).getByText(/100 entrées, 2.?000 octets sur un quota de 268.?435.?456/)).toBeInTheDocument();
    expect(within(region).getAllByText(/quota dépassé/)).toHaveLength(1);
  });

  // req: ENF-OBS-005
  it('offers the diagnostic bundle as a download of the server file', async () => {
    server.use(serve(page()));
    render(Page);
    const link = await screen.findByRole('link', { name: 'Télécharger le paquet de diagnostic' });
    expect(link).toHaveAttribute('href', '/api/v1/diagnostic/bundle');
    expect(link).toHaveAttribute('download');
  });

  // req: ENF-OBS-005
  it('shows the refusal and nothing of the page to a member or a child', async () => {
    server.use(
      http.get(`${BASE}/api/v1/health/page`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/forbidden', title: 'Accès refusé', status: 403, code: 'forbidden' },
          { status: 403 }
        )
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Accès refusé');
    expect(screen.queryByRole('link', { name: /diagnostic/ })).toBeNull();
    expect(screen.queryByText(/Sources|irremplaçable|Kavita/)).toBeNull();
  });

  // req: ENF-OBS-005
  it('drops an entry that has no stable code instead of showing it half-way', async () => {
    server.use(serve(page({ kavita_audit: [{ subject: '01TG' }, { code: 7 }] })));
    render(Page);
    expect(await screen.findByText(/Aucun compte Kavita ne voit plus/)).toBeInTheDocument();
    expect(screen.queryByText('01TG')).toBeNull();
  });
});
