import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import type { ActivityOut } from '#lib/api/types.gen.ts';
import ActivityItem from './ActivityItem.svelte';

const KAVITA = 'http://kavita.test:5000';

function item(over: Partial<ActivityOut> = {}): ActivityOut {
  return {
    event_id: 1,
    type: 'ops.request_arrived',
    visibility: 'household',
    payload: {},
    occurred_at: '2026-11-03T09:00:00Z',
    title: 'Un titre',
    title_masked: false,
    family_state: 'available',
    open_url: KAVITA,
    koreader_reset: false,
    ...over
  } as ActivityOut;
}

describe('ActivityItem', () => {
  // req: EF-UX-010
  it('test_activity_family_single_state_with_open_in_kavita', () => {
    render(ActivityItem, { item: item() });
    expect(screen.getByText('Disponible dans Kavita')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Ouvrir dans Kavita' })).toHaveAttribute('href', KAVITA);
    expect(screen.queryByText(/Étape/)).toBeNull();
    expect(screen.queryByText(/KOReader/)).toBeNull();
  });

  // req: EF-UX-010
  it('test_activity_family_waiting_and_preparing_have_no_link', () => {
    for (const [state, label] of [
      ['preparing', 'En préparation'],
      ['waiting', 'En attente']
    ] as const) {
      const { unmount } = render(ActivityItem, { item: item({ family_state: state, open_url: null }) });
      expect(screen.getByText(label)).toBeInTheDocument();
      expect(screen.queryByRole('link')).toBeNull();
      unmount();
    }
  });

  // req: EF-UX-010
  it('test_activity_shows_koreader_reset', () => {
    render(ActivityItem, {
      item: item({
        type: 'unit.upgraded',
        visibility: 'ops',
        payload: { reason: 'better_version', koreader_reset: true },
        family_state: null,
        open_url: null,
        koreader_reset: true
      })
    });
    expect(screen.getByText('Position KOReader réinitialisée')).toBeInTheDocument();
  });

  // req: EF-UX-010
  it('test_activity_admin_reads_the_step', () => {
    render(ActivityItem, {
      item: item({
        type: 'download_request',
        payload: { state: 'downloaded' },
        family_state: 'preparing',
        open_url: null
      })
    });
    expect(screen.getByText('Étape : downloaded')).toBeInTheDocument();
  });

  // req: ENF-SEC-010
  it('test_activity_item_never_decides_who_sees_what', () => {
    // the screen shows what the server sent: a family item has no flag, so no word of it
    render(ActivityItem, { item: item({ payload: { koreader_reset: false } }) });
    expect(screen.queryByText(/KOReader/)).toBeNull();
  });

  // req: EF-UX-010
  it('test_activity_refuses_a_link_that_is_not_a_web_address', () => {
    render(ActivityItem, { item: item({ open_url: 'javascript:alert(1)' }) });
    expect(screen.queryByRole('link')).toBeNull();
  });

  // req: ENF-SEC-010
  it('test_activity_masked_title_says_so_in_the_catalogue', () => {
    render(ActivityItem, { item: item({ title: null, title_masked: true }) });
    expect(screen.getByText('Titre masqué')).toBeInTheDocument();
  });
});
