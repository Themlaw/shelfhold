import { render, screen, within } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import { createRawSnippet } from 'svelte';
import Layout from '../../routes/+layout.svelte';
import Nav from './Nav.svelte';

const links = () => within(screen.getByRole('navigation')).getAllByRole('link').map((a) => a.textContent?.trim());

describe('Nav (EF-UX-005)', () => {
  // req: EF-UX-005
  it('test_nav_by_role: a member sees the family tabs and no admin tab', () => {
    render(Nav, { ids: ['home', 'search', 'requests'] });
    expect(links()).toEqual(['Accueil', 'Chercher', 'Mes demandes']);
    expect(screen.queryByRole('link', { name: 'Activité' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Réglages' })).toBeNull();
  });

  // req: EF-UX-005
  it('an admin also sees À approuver, Activité and Réglages', () => {
    render(Nav, { ids: ['home', 'search', 'requests', 'approve', 'activity', 'settings'] });
    expect(links()).toEqual(['Accueil', 'Chercher', 'Mes demandes', 'À approuver', 'Activité', 'Réglages']);
  });

  // req: EF-UX-005
  it('marks the current tab and renders nothing for nobody signed in', () => {
    const { unmount } = render(Nav, { ids: ['home', 'search'], current: '/chercher' });
    expect(screen.getByRole('link', { name: 'Chercher' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Accueil' })).not.toHaveAttribute('aria-current');
    unmount();
    render(Nav, { ids: [] });
    expect(screen.queryByRole('navigation')).toBeNull();
  });

  // req: EF-UX-005
  it('the root layout shows the tabs of its data, and none when nobody is signed in', () => {
    const page = createRawSnippet(() => ({ render: () => '<main></main>' }));
    const { unmount } = render(Layout, {
      children: page,
      data: { sourceUrl: null, appearance: null, nav: ['home', 'activity'] }
    } as never);
    expect(links()).toEqual(['Accueil', 'Activité']);
    unmount();
    render(Layout, { children: page, data: { sourceUrl: null, appearance: null, nav: [] } } as never);
    expect(screen.queryByRole('navigation')).toBeNull();
  });
});
