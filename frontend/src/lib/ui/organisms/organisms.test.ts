import { createRawSnippet } from 'svelte';
import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import AppHeader from './AppHeader.svelte';
import Colophon from './Colophon.svelte';
import DataTable from './DataTable.svelte';
import ErrorScreen, { errorKind } from './ErrorScreen.svelte';
import MobileTabBar from './MobileTabBar.svelte';
import SettingsForm from './SettingsForm.svelte';
import Shelf from './Shelf.svelte';
import TableOfContents from './TableOfContents.svelte';

const html = (markup: string) => createRawSnippet(() => ({ render: () => markup }));

describe('AppHeader', () => {
  // req: EF-UX-005
  it('signed in: the house, the search of the catalogue and the account of the person', () => {
    render(AppHeader, { user: { name: 'Claire', role: 'admin' } });
    expect(screen.getByRole('banner')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Shelfhold/ })).toHaveAttribute('href', '/');
    expect(screen.getByRole('search')).toHaveAttribute('action', '/chercher');
    expect(screen.getByRole('button', { name: 'Compte de Claire (Administrateur)' })).toHaveAttribute('aria-haspopup', 'menu');
    expect(screen.queryByRole('link', { name: 'Se connecter' })).toBeNull();
  });

  // req: EF-UX-005
  it('a child reads « La bibliothèque de » her name', () => {
    render(AppHeader, { user: { name: 'Louise', role: 'child' } });
    expect(screen.getByText('La bibliothèque de Louise')).toBeInTheDocument();
  });

  // req: EF-UX-012
  it('the scheme button opens a menu of three choices, the current one checked', async () => {
    render(AppHeader, { user: null, scheme: 'dark' });
    const button = screen.getByRole('button', { name: /Apparence/ });
    expect(button.querySelector('[data-icon="moon"]')).not.toBeNull();
    await userEvent.click(button);
    const items = await vi.waitFor(() => {
      const found = screen.queryAllByRole('menuitemradio', { hidden: true });
      if (found.length !== 3) throw new Error('menu closed');
      return found;
    });
    expect(items.map((i) => [i.textContent?.trim(), i.getAttribute('aria-checked')])).toEqual([
      ['Clair', 'false'],
      ['Sombre', 'true'],
      ['Comme le système', 'false']
    ]);
  });
});

describe('TableOfContents and MobileTabBar', () => {
  const groups = [
    {
      label: 'Lire',
      entries: [
        { href: '/', label: 'Accueil', icon: 'home' as const },
        { href: '/mes-demandes', label: 'Mes demandes', icon: 'inbox' as const, folio: 3, alert: true }
      ]
    }
  ];

  // req: EF-UX-005
  it('marks the current entry, and says a folio waits for an action in words', () => {
    render(TableOfContents, { groups, current: '/mes-demandes', label: 'Navigation principale', title: 'Table des matières' });
    const nav = screen.getByRole('navigation', { name: 'Navigation principale' });
    const current = within(nav).getByRole('link', { name: /Mes demandes/ });
    expect(current).toHaveAttribute('aria-current', 'page');
    expect(current).toHaveTextContent('3 à traiter');
    expect(within(nav).getByRole('link', { name: 'Accueil' })).not.toHaveAttribute('aria-current');
  });

  // req: ENF-A11Y-004
  it('the tab bar marks its current tab and opens the rest with « Plus »', async () => {
    const onmore = vi.fn();
    render(MobileTabBar, { entries: groups[0]!.entries, current: '/', label: 'Navigation mobile', onmore });
    const bar = screen.getByRole('navigation', { name: 'Navigation mobile' });
    expect(within(bar).getByRole('link', { name: 'Accueil' })).toHaveAttribute('aria-current', 'page');
    const more = within(bar).getByRole('button', { name: 'Plus' });
    expect(more).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(more);
    expect(onmore).toHaveBeenCalled();
  });
});

describe('Colophon', () => {
  // req: ENF-LEG-012
  it('prints when the page was set, the reader-light link and the source of the running version', () => {
    render(Colophon, { sourceUrl: 'https://example.org/src', printedAt: new Date(2026, 10, 25, 21, 4) });
    const footer = screen.getByRole('contentinfo');
    expect(footer).toHaveTextContent('Achevé d’afficher le mercredi 25 novembre 2026 à 21:04.');
    expect(within(footer).getByRole('link', { name: 'Code source' })).toHaveAttribute('href', 'https://example.org/src');
    expect(within(footer).getByRole('link', { name: 'Version liseuse' })).toHaveAttribute('data-sveltekit-reload');
  });
});

describe('Shelf and DataTable', () => {
  // req: ENF-A11Y-001
  it('a shelf is a named list of volumes', () => {
    render(Shelf, { label: 'Arrivé récemment', busy: true, children: html('<div role="listitem">Un</div>') });
    const shelf = screen.getByRole('list', { name: 'Arrivé récemment' });
    expect(within(shelf).getAllByRole('listitem')).toHaveLength(1);
    expect(shelf).toHaveAttribute('aria-busy', 'true');
  });

  // req: ENF-A11Y-004
  it('a register: a caption, column headers, a label on every cell for the phone, the empty state', () => {
    const columns = [
      { key: 'title', label: 'Titre', primary: true },
      { key: 'size', label: 'Taille', num: true },
      { key: 'go', label: 'Actions', actions: true, hideLabel: true }
    ];
    const { unmount } = render(DataTable, {
      caption: 'Exports',
      columns,
      rows: [{ id: 1, title: 'Famille', size: '2 Go', go: '' }],
      key: (row: { id: number }) => row.id,
      selected: () => true
    } as never);
    const table = screen.getByRole('table', { name: 'Exports' });
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent?.trim())).toEqual(['Titre', 'Taille', 'Actions']);
    const cells = within(table).getAllByRole('cell');
    expect(cells[0]).not.toHaveAttribute('data-label'); // the name of the row
    expect(cells[1]).toHaveAttribute('data-label', 'Taille');
    expect(cells[1]).toHaveClass('num');
    expect(within(table).getAllByRole('row')[1]).toHaveAttribute('aria-selected', 'true');
    unmount();
    render(DataTable, { caption: 'Exports', columns, rows: [], key: () => 0, empty: html('<p>Aucun export</p>') } as never);
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.getByText('Aucun export')).toBeInTheDocument();
  });
});

describe('SettingsForm', () => {
  // req: ENF-A11Y-004
  it('is a named form whose save button ends it, keeps its label while saving, and a danger zone apart', async () => {
    const onsubmit = vi.fn();
    render(SettingsForm, {
      label: 'Notifications',
      saveLabel: 'Enregistrer',
      onsubmit,
      children: html('<div class="setting">Réglage</div>'),
      danger: html('<p>Supprimer le profil</p>'),
      dangerTitle: 'Zone dangereuse'
    });
    expect(screen.getByRole('form', { name: 'Notifications' })).toBeInTheDocument();
    const save = screen.getByRole('button', { name: 'Enregistrer' });
    expect(save).toHaveAttribute('type', 'submit');
    expect(save).toHaveClass('main-action');
    await userEvent.click(save);
    expect(onsubmit).toHaveBeenCalledOnce();
    expect(screen.getByRole('region', { name: 'Zone dangereuse' })).toHaveTextContent('Supprimer le profil');
  });
});

describe('ErrorScreen', () => {
  // req: EF-UX-003
  it('tells the kind of the failure from the status and the network', () => {
    expect(errorKind(404, true)).toBe('not-found');
    expect(errorKind(403, true)).toBe('forbidden');
    expect(errorKind(401, true)).toBe('signed-out');
    expect(errorKind(500, true)).toBe('server');
    expect(errorKind(404, false)).toBe('offline');
  });

  // req: EF-UX-003
  it('404: the missing volume, a search and the way home, never the raw message', () => {
    render(ErrorScreen, { status: 404, path: '/serie/7f3a', signedIn: true });
    expect(screen.getByRole('heading', { level: 1, name: 'Ce volume n’est pas sur l’étagère' })).toBeInTheDocument();
    expect(screen.getByText('/serie/7f3a')).toBeInTheDocument();
    expect(screen.getByRole('search')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Revenir à l’accueil' })).toHaveAttribute('href', '/');
    expect(document.body.textContent).not.toMatch(/[{}]|Not Found/);
  });

  // req: EF-UX-003, EF-UX-005
  it('403 for a child: shelved higher, and « ask a parent »', () => {
    render(ErrorScreen, { status: 403, signedIn: true, child: true });
    expect(screen.getByRole('heading', { level: 1, name: 'Ce livre est rangé plus haut que ta bibliothèque' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Demander à un parent' })).toHaveAttribute('href', '/chercher');
    expect(screen.getByRole('link', { name: 'Retour à ma bibliothèque' })).toHaveAttribute('href', '/');
  });

  // req: EF-UX-003
  it('401: the sign-in page of the server; 500: try again with the incident id', () => {
    const { unmount } = render(ErrorScreen, { status: 401 });
    expect(screen.getByRole('link', { name: 'Se connecter' })).toHaveAttribute('data-sveltekit-reload');
    unmount();
    render(ErrorScreen, { status: 500, incident: 'req-9' });
    expect(screen.getByRole('heading', { level: 1, name: 'Une étagère s’est effondrée' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Réessayer' })).toBeInTheDocument();
    expect(screen.getByText('req-9')).toBeInTheDocument();
  });
});
