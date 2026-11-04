import { render, screen } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import HealthStatus from './HealthStatus.svelte';

const server = setupServer();
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: 'http://shelfhold.test' });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('HealthStatus', () => {
  // req: ENF-OPS-003
  it('shows a loading message first', () => {
    server.use(http.get('http://shelfhold.test/healthz', () => new Promise(() => {})));
    render(HealthStatus);
    expect(screen.getByText('Vérification en cours…')).toBeInTheDocument();
  });

  // req: ENF-OPS-003
  it('shows the server state when /healthz answers ok', async () => {
    server.use(
      http.get('http://shelfhold.test/healthz', () => HttpResponse.json({ state: 'ok' }))
    );
    render(HealthStatus);
    expect(await screen.findByText('Le serveur répond.')).toBeInTheDocument();
  });

  // req: ENF-OPS-003, ENF-I18N-001
  it('shows the migration progress with an ICU plural', async () => {
    server.use(
      http.get('http://shelfhold.test/healthz', () =>
        HttpResponse.json({ state: 'migrating', progress: { done: 1, total: 4 } })
      )
    );
    render(HealthStatus);
    expect(
      await screen.findByText('Migration en cours : 1 étape terminée sur 4.')
    ).toBeInTheDocument();
  });

  // req: ENF-OPS-003
  it('shows an alert when the server is unreachable', async () => {
    server.use(http.get('http://shelfhold.test/healthz', () => HttpResponse.error()));
    render(HealthStatus);
    expect(await screen.findByRole('alert')).toHaveTextContent('Le serveur est injoignable.');
  });
});
