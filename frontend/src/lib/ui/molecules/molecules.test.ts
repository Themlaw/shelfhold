import { createRawSnippet, flushSync } from 'svelte';
import { render, screen, waitFor, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { dismiss, NOTICE_MS, notify } from '../toast.svelte.ts';
import Alert from './Alert.svelte';
import ChapterRow from './ChapterRow.svelte';
import ConfirmDialog from './ConfirmDialog.svelte';
import Dialog from './Dialog.svelte';
import EmptyState from './EmptyState.svelte';
import ErrorState from './ErrorState.svelte';
import Field from './Field.svelte';
import Pagination, { pageWindow } from './Pagination.svelte';
import SearchBar from './SearchBar.svelte';
import SeriesCard from './SeriesCard.svelte';
import SettingRow from './SettingRow.svelte';
import Tabs from './Tabs.svelte';
import Toast from './Toast.svelte';

const text = (value: string) => createRawSnippet(() => ({ render: () => `<span>${value}</span>` }));
/** A control that takes what a Field or a SettingRow gives it. */
const control = createRawSnippet((c: () => { id: string; 'aria-describedby'?: string; 'aria-invalid'?: boolean }) => ({
  render: () =>
    `<input id="${c().id}"${c()['aria-describedby'] ? ` aria-describedby="${c()['aria-describedby']}"` : ''}${c()['aria-invalid'] ? ' aria-invalid="true"' : ''}>`
}));

describe('Field', () => {
  // req: ENF-A11Y-001
  it('names its control by the label, describes it by the hint and the error', () => {
    render(Field, { label: 'Nom de la liste', hint: 'Visible par la famille', error: 'Ce nom existe déjà.', optional: true, children: control as never });
    const input = screen.getByRole('textbox', { name: /Nom de la liste/ });
    expect(input).toHaveAccessibleName('Nom de la liste facultatif');
    expect(input).toHaveAccessibleDescription('Visible par la famille Ce nom existe déjà.');
    expect(input).toHaveAttribute('aria-invalid', 'true');
  });
});

describe('SettingRow', () => {
  // req: ENF-A11Y-001
  it('labels its control and describes it by its help', () => {
    render(SettingRow, { label: 'Langue', help: 'La langue de l’interface', children: control as never });
    expect(screen.getByRole('textbox', { name: 'Langue' })).toHaveAccessibleDescription('La langue de l’interface');
  });
});

describe('Alert, EmptyState, ErrorState', () => {
  // req: EF-UX-003
  it('an alert announced at once is an alert; one shown with the page is not live', () => {
    const { unmount } = render(Alert, { tone: 'danger', title: 'Refusé', live: 'assertive', incident: 'req-42' });
    expect(screen.getByRole('alert')).toHaveTextContent('Refusé');
    expect(screen.getByText('req-42')).toBeInTheDocument();
    unmount();
    render(Alert, { tone: 'info', title: 'À savoir' });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
  });

  // req: EF-UX-008
  it('an empty state says what the place is for and gives the next action', () => {
    const actions = createRawSnippet(() => ({ render: () => '<a href="/chercher">Chercher un livre</a>' }));
    render(EmptyState, { title: 'Aucune demande', headingLevel: 3, actions });
    expect(screen.getByRole('heading', { level: 3, name: 'Aucune demande' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Chercher un livre' })).toHaveAttribute('href', '/chercher');
  });

  // req: EF-UX-003
  it('an error state has one heading, the code as a folio and the incident id', () => {
    render(ErrorState, { kind: 'server', title: 'Une étagère s’est effondrée', code: 'Erreur 500', incident: 'abc-1' });
    expect(screen.getByRole('heading', { level: 1, name: 'Une étagère s’est effondrée' })).toBeInTheDocument();
    expect(screen.getByText('Erreur 500')).toBeInTheDocument();
    expect(screen.getByText('abc-1')).toBeInTheDocument();
  });
});

describe('SearchBar', () => {
  // req: ENF-A11Y-002
  it('is a search landmark; submitting gives the trimmed text; « / » focuses it', async () => {
    const onsearch = vi.fn();
    render(SearchBar, { label: 'Chercher dans la bibliothèque', shortcut: true, onsearch });
    expect(screen.getByRole('search')).toBeInTheDocument();
    const field = screen.getByRole('searchbox', { name: 'Chercher dans la bibliothèque' });
    await userEvent.keyboard('/');
    expect(field).toHaveFocus();
    await userEvent.keyboard('  Nemo  {Enter}');
    expect(onsearch).toHaveBeenCalledWith('Nemo');
  });

  // req: ENF-A11Y-002
  it('without a handler, it is a GET form to its action', () => {
    render(SearchBar, { label: 'Chercher', action: '/chercher' });
    const form = screen.getByRole('search');
    expect(form).toHaveAttribute('action', '/chercher');
    expect(form).toHaveAttribute('method', 'get');
    expect(screen.getByRole('searchbox')).toHaveAttribute('name', 'q');
  });
});

describe('Pagination', () => {
  // req: ENF-A11Y-002
  it('keeps the first, the last and the neighbours of the current page', () => {
    expect(pageWindow(1, 5)).toEqual([1, 2, 3, 4, 5]);
    expect(pageWindow(6, 20)).toEqual([1, null, 5, 6, 7, null, 20]);
  });

  // req: ENF-A11Y-002
  it('the page is in the address; the current one is marked; no previous on the first', () => {
    render(Pagination, { page: 1, pages: 9, href: (p: number) => `/bibliotheque?page=${p}` });
    const nav = screen.getByRole('navigation', { name: 'Pagination' });
    expect(within(nav).getByText('1')).toHaveAttribute('aria-current', 'page');
    expect(within(nav).getByRole('link', { name: 'Page 2' })).toHaveAttribute('href', '/bibliotheque?page=2');
    expect(within(nav).queryByRole('link', { name: /Page précédente/ })).toBeNull();
    expect(within(nav).getByRole('link', { name: /Page suivante/ })).toHaveAttribute('rel', 'next');
  });
});

describe('ChapterRow and SeriesCard', () => {
  // req: ENF-A11Y-001
  it('the chapter being read is marked; a locked one is not a link', () => {
    render(ChapterRow, { number: 19, title: 'Vanikoro', href: '/lire/1', state: 'current' });
    render(ChapterRow, { number: 20, title: 'Le détroit', href: '/lire/2', state: 'locked' });
    expect(screen.getByRole('link', { name: 'Vanikoro' })).toHaveAttribute('aria-current', 'true');
    expect(screen.queryByRole('link', { name: 'Le détroit' })).toBeNull();
  });

  // req: ENF-A11Y-002
  it('a volume is one stop in the tab order, named by its title; a locked one tells nothing', () => {
    const { unmount } = render(SeriesCard, { title: 'L’Île mystérieuse', href: '/serie/1', author: 'Jules Verne' });
    expect(screen.getAllByRole('link')).toHaveLength(1);
    expect(screen.getByRole('link', { name: 'L’Île mystérieuse' })).toHaveAttribute('href', '/serie/1');
    unmount();
    const { container } = render(SeriesCard, { title: 'Titre secret', href: '/serie/2', locked: true });
    expect(container.textContent).not.toContain('Titre secret');
    expect(screen.queryByRole('link')).toBeNull();
  });
});

describe('Toast', () => {
  afterEach(() => {
    dismiss();
    vi.useRealTimers();
  });

  // req: ENF-A11Y-001
  it('a notice is said politely and goes away by itself; a danger is an alert and stays', async () => {
    vi.useFakeTimers();
    render(Toast);
    notify({ tone: 'success', title: 'Liste enregistrée' });
    flushSync();
    expect(screen.getByRole('status')).toHaveTextContent('Liste enregistrée');
    vi.advanceTimersByTime(NOTICE_MS + 10);
    flushSync();
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
    notify({ tone: 'danger', title: 'La déconnexion a échoué' });
    flushSync();
    expect(screen.getByRole('alert')).toHaveTextContent('La déconnexion a échoué');
    vi.advanceTimersByTime(NOTICE_MS * 3);
    flushSync();
    expect(screen.getByRole('alert')).toHaveTextContent('La déconnexion a échoué');
  });

  // req: ENF-A11Y-002
  it('can be closed by its button', async () => {
    render(Toast);
    notify({ tone: 'warning', title: 'Hors ligne' });
    flushSync();
    await userEvent.click(screen.getByRole('button', { name: 'Fermer le message' }));
    expect(screen.queryByText('Hors ligne')).toBeNull();
  });
});

describe('Dialog and ConfirmDialog', () => {
  // req: ENF-A11Y-002
  it('a dialog is named by its title and Escape closes it', async () => {
    const onclose = vi.fn();
    render(Dialog, { open: true, title: 'Table des matières', children: text('Contenu'), onclose });
    const dialog = await screen.findByRole('dialog', { name: 'Table des matières' });
    expect(dialog).toHaveTextContent('Contenu');
    await userEvent.keyboard('{Escape}');
    // the closing transition may still hold the node for a frame on a slow runner
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(onclose).toHaveBeenCalled();
  });

  // req: ENF-A11Y-002
  it('the typed confirmation stays dead until the phrase matches, then confirms once', async () => {
    const onconfirm = vi.fn();
    render(ConfirmDialog, {
      open: true,
      title: 'Supprimer la liste « Été » ?',
      confirmLabel: 'Supprimer',
      phrase: 'supprimer Été',
      onconfirm
    });
    const confirm = await screen.findByRole('button', { name: 'Supprimer' });
    await userEvent.click(confirm);
    expect(onconfirm).not.toHaveBeenCalled();
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    await userEvent.type(screen.getByRole('textbox'), 'supprimer Été');
    await userEvent.click(confirm);
    expect(onconfirm).toHaveBeenCalledWith('supprimer Été');
  });
});

describe('Tabs', () => {
  // req: ENF-A11Y-002
  it('with panels: arrows move between tabs, each tab owns its panel', async () => {
    const panel = createRawSnippet((item: () => { label: string }) => ({ render: () => `<p>Panneau ${item().label}</p>` }));
    render(Tabs, {
      label: 'Sections',
      items: [
        { value: 'a', label: 'Volumes' },
        { value: 'b', label: 'Chapitres' }
      ],
      panel
    });
    const first = screen.getByRole('tab', { name: 'Volumes' });
    expect(first).toHaveAttribute('aria-selected', 'true');
    first.focus();
    await userEvent.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: 'Chapitres' })).toHaveFocus();
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Panneau Chapitres');
  });

  // req: ENF-A11Y-002
  it('with addresses: a navigation, the current page marked, the long label kept as the name', () => {
    render(Tabs, {
      label: 'Réglages',
      value: 'b',
      items: [
        { value: 'a', label: 'Bibliothèques', short: 'Biblio.', href: '/reglages/etageres' },
        { value: 'b', label: 'Notifications', href: '/reglages/notifications' }
      ]
    });
    const nav = screen.getByRole('navigation', { name: 'Réglages' });
    expect(within(nav).getByRole('link', { name: 'Notifications' })).toHaveAttribute('aria-current', 'page');
    expect(within(nav).getByRole('link', { name: 'Bibliothèques' })).not.toHaveAttribute('aria-current');
  });
});
