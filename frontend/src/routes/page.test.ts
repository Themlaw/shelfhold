import { render, screen } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const server = setupServer(
  http.get('http://shelfhold.test/healthz', () => HttpResponse.json({ state: 'ok', version: '0.0.0' }))
);
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: 'http://shelfhold.test' });
});
afterAll(() => server.close());

describe('home page', () => {
  it('has the pre-alpha title as level-one heading', async () => {
    render(Page);
    expect(screen.getByRole('heading', { level: 1, name: 'Shelfhold — pré-alpha' })).toBeInTheDocument();
    await screen.findByText(/Le serveur répond/);
  });

  it('calls /healthz through the generated client', async () => {
    render(Page);
    expect(await screen.findByText('Le serveur répond (version 0.0.0).')).toBeInTheDocument();
  });
});
