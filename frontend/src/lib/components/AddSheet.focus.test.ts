import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { setupServer } from 'msw/node';
import { http, HttpResponse } from 'msw';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import AddSheet from './AddSheet.svelte';

// The sheet declares `aria-modal`: focus goes round inside it and never to the page behind.
const BASE = 'http://shelfhold.test';
const server = setupServer(
  http.get(`${BASE}/api/v1/shelves`, () => HttpResponse.json({ items: [] })),
  http.get(`${BASE}/api/v1/sources/alpha/series/a1/estimate`, () =>
    HttpResponse.json({ mode: 'all', units: 3, size_bytes: 3000, free_bytes: 10 ** 9, needs_confirmation: false })
  )
);

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' });
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Add sheet, focus', () => {
  it('keeps Tab and Shift+Tab inside the sheet', async () => {
    const outside = document.createElement('button');
    document.body.append(outside);
    render(AddSheet, {
      edition: { source_key: 'alpha', remote_id: 'a1', title: 'Un livre', format: 'text' },
      sourceName: 'Alpha',
      onclose: vi.fn(),
      onadded: vi.fn()
    });
    const user = userEvent.setup();
    const dialog = await screen.findByRole('dialog');
    const close = screen.getByRole('button', { name: 'Fermer' });
    const submit = await screen.findByRole('button', { name: 'Ajouter à ma bibliothèque' });
    expect(dialog).toHaveFocus();

    await user.tab({ shift: true }); // from the sheet itself, backwards: the last control
    expect(dialog.contains(document.activeElement)).toBe(true);
    close.focus();
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(submit.isConnected ? lastFocusable(dialog) : close);
    await user.tab(); // from the last control, forwards: the first
    expect(document.activeElement).toBe(close);
    expect(document.activeElement).not.toBe(outside);
    outside.remove();
  });
});

function lastFocusable(root: HTMLElement): HTMLElement {
  const all = Array.from(root.querySelectorAll<HTMLElement>('button:not([disabled]), input, select, a[href]'));
  return all[all.length - 1]!;
}
