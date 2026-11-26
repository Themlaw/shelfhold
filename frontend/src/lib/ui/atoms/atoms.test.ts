import { createRawSnippet } from 'svelte';
import { fireEvent, render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import Avatar, { initials } from './Avatar.svelte';
import Badge from './Badge.svelte';
import Button from './Button.svelte';
import Checkbox from './Checkbox.svelte';
import Cover from './Cover.svelte';
import { coverKind, coverPlan } from './cover.ts';
import Icon from './Icon.svelte';
import IconButton from './IconButton.svelte';
import { ICONS, MOTIFS } from './icons.ts';
import Illustration from './Illustration.svelte';
import Input from './Input.svelte';
import Link from './Link.svelte';
import Progress from './Progress.svelte';
import ProgressRibbon from './ProgressRibbon.svelte';
import Radio from './Radio.svelte';
import Select from './Select.svelte';
import Skeleton from './Skeleton.svelte';
import Spinner from './Spinner.svelte';
import Switch from './Switch.svelte';
import Textarea from './Textarea.svelte';

const text = (value: string) => createRawSnippet(() => ({ render: () => `<span>${value}</span>` }));

describe('Button', () => {
  // req: ENF-A11Y-002
  it('is a real button of type "button" by default, and a link when it has an address', () => {
    const { unmount } = render(Button, { children: text('Enregistrer'), variant: 'primary' });
    const button = screen.getByRole('button', { name: 'Enregistrer' });
    expect(button).toHaveAttribute('type', 'button');
    expect(button).toHaveClass('btn--primary');
    unmount();
    render(Button, { children: text('Ouvrir'), href: '/bibliotheque' });
    expect(screen.getByRole('link', { name: 'Ouvrir' })).toHaveAttribute('href', '/bibliotheque');
  });

  // req: ENF-A11Y-002
  it('loading keeps its label, says it is busy and does not act twice', async () => {
    const onclick = vi.fn();
    render(Button, { children: text('Enregistrer'), loading: true, onclick });
    const button = screen.getByRole('button', { name: 'Enregistrer' });
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(button).toHaveAttribute('aria-disabled', 'true');
    await userEvent.click(button);
    expect(onclick).not.toHaveBeenCalled();
  });

  // req: ENF-A11Y-002
  it('disabled stays in the tab order (aria-disabled) and ignores clicks', async () => {
    const onclick = vi.fn();
    render(Button, { children: text('Supprimer'), variant: 'danger', disabled: true, onclick });
    const button = screen.getByRole('button', { name: 'Supprimer' });
    expect(button).not.toHaveAttribute('disabled');
    expect(button).toHaveAttribute('aria-disabled', 'true');
    button.focus();
    expect(button).toHaveFocus();
    await userEvent.click(button);
    expect(onclick).not.toHaveBeenCalled();
  });
});

describe('IconButton', () => {
  // req: ENF-A11Y-001
  it('has the name it is given, a tooltip, and says when it is pressed', () => {
    render(IconButton, { icon: 'grid', label: 'Couvertures', pressed: true });
    const button = screen.getByRole('button', { name: 'Couvertures' });
    expect(button).toHaveAttribute('title', 'Couvertures');
    expect(button).toHaveAttribute('aria-pressed', 'true');
    expect(button.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  });
});

describe('Icon', () => {
  // req: ENF-A11Y-001
  it('is decorative unless it stands alone with a label', () => {
    const { container, unmount } = render(Icon, { name: 'search' });
    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    unmount();
    render(Icon, { name: 'alert', label: 'Attention' });
    expect(screen.getByRole('img', { name: 'Attention' })).toBeInTheDocument();
  });

  // req: ENF-PRIV-001
  it('is drawn from the house set, never fetched', () => {
    expect(Object.keys(ICONS).length).toBeGreaterThanOrEqual(60);
    expect(Object.keys(MOTIFS)).toHaveLength(12);
    const { container } = render(Icon, { name: 'books' });
    expect(container.innerHTML).not.toMatch(/https?:|<use|<image/);
  });
});

describe('form controls', () => {
  // req: ENF-A11Y-001
  it('Input and Textarea mark an invalid value for assistive technology', () => {
    render(Input, { 'aria-label': 'Nom', invalid: true, value: 'x' });
    expect(screen.getByRole('textbox', { name: 'Nom' })).toHaveAttribute('aria-invalid', 'true');
    render(Textarea, { 'aria-label': 'Motif', invalid: false });
    expect(screen.getByRole('textbox', { name: 'Motif' })).not.toHaveAttribute('aria-invalid', 'true');
  });

  // req: ENF-A11Y-002
  it('Select is a native select with its options, the keyboard of the system', async () => {
    render(Select, {
      'aria-label': 'Trier par',
      value: 'recent',
      options: [
        { value: 'recent', label: 'Ajout récent' },
        { value: 'title', label: 'Titre' }
      ]
    });
    const select = screen.getByRole('combobox', { name: 'Trier par' });
    expect(select).toHaveValue('recent');
    await userEvent.selectOptions(select, 'title');
    expect(select).toHaveValue('title');
  });

  // req: ENF-A11Y-002
  it('Checkbox has its label, a hint that describes it, and the mixed state', async () => {
    render(Checkbox, { label: 'Tout choisir', hint: '12 volumes', indeterminate: true });
    const box = screen.getByRole('checkbox', { name: 'Tout choisir' });
    expect(box).toHaveAccessibleDescription('12 volumes');
    expect((box as HTMLInputElement).indeterminate).toBe(true);
    await userEvent.click(box);
    expect(box).toBeChecked();
  });

  // req: ENF-A11Y-002
  it('Radio buttons of a group are one choice', async () => {
    render(Radio, { name: 'scheme', value: 'light', label: 'Clair', group: 'dark' });
    render(Radio, { name: 'scheme', value: 'dark', label: 'Sombre', group: 'dark' });
    expect(screen.getByRole('radio', { name: 'Sombre' })).toBeChecked();
    await userEvent.click(screen.getByRole('radio', { name: 'Clair' }));
    expect(screen.getByRole('radio', { name: 'Clair' })).toBeChecked();
  });

  // req: ENF-A11Y-002
  it('Switch is a switch, toggled by the keyboard', async () => {
    render(Switch, { label: 'Notifications', hint: 'Un message par arrivée' });
    const toggle = screen.getByRole('switch', { name: 'Notifications' });
    expect(toggle).not.toBeChecked();
    toggle.focus();
    await userEvent.keyboard(' ');
    expect(toggle).toBeChecked();
  });
});

describe('Badge, Avatar, Link', () => {
  // req: ENF-A11Y-001
  it('a status badge is an icon and a word, never a colour alone', () => {
    const { container } = render(Badge, { tone: 'success', children: text('Lu') });
    expect(container.querySelector('svg[data-icon="check"]')).not.toBeNull();
    expect(container.textContent).toContain('Lu');
  });

  // req: ENF-A11Y-001
  it('the ex-libris prints initials, and is named only when it stands alone', () => {
    expect(initials('Claire Martin')).toBe('CM');
    expect(initials('louise')).toBe('L');
    expect(initials('  ')).toBe('?');
    render(Avatar, { name: 'Jules', kind: 'child', label: 'Profil de Jules' });
    expect(screen.getByRole('img', { name: 'Profil de Jules' })).toHaveTextContent('J');
  });

  // req: ENF-A11Y-001, ENF-SEC-004
  it('an external link says it opens a new tab and gives no access to the opener', () => {
    render(Link, { href: 'https://example.org/aide', external: true, children: text('Aide') });
    const link = screen.getByRole('link', { name: /Aide/ });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));
    expect(link).toHaveAccessibleName(expect.stringContaining('nouvel onglet'));
  });
});

describe('Spinner, Skeleton, Progress', () => {
  // req: ENF-A11Y-001
  it('a named spinner is a status; a skeleton is hidden from assistive technology', () => {
    render(Spinner, { label: 'Chargement' });
    expect(screen.getByRole('status')).toHaveTextContent('Chargement');
    const { container } = render(Skeleton, { shape: 'cover' });
    expect(container.firstElementChild).toHaveAttribute('aria-hidden', 'true');
  });

  // req: ENF-A11Y-001
  it('Progress is a named progressbar, clamped between 0 and 100', () => {
    render(Progress, { value: 1.4, label: 'Lecture' });
    const bar = screen.getByRole('progressbar', { name: 'Lecture' });
    expect(bar).toHaveAttribute('aria-valuenow', '100');
    // the ribbon is a picture of a number said elsewhere: an image when named, at least a visible tab
    render(ProgressRibbon, { value: 0.4, label: 'Lu à 40 %' });
    expect(screen.getByRole('img', { name: 'Lu à 40 %' })).toHaveAttribute('data-length', '40');
    const { container } = render(ProgressRibbon, { value: 0 });
    expect(container.querySelector('.ribbon')).toHaveAttribute('data-length', '6');
  });
});

describe('Cover', () => {
  // req: EF-UX-008
  it('without an image, draws a cover from the title: same title, same cover', () => {
    expect(coverPlan('Le Tour du monde')).toEqual(coverPlan('Le Tour du monde'));
    expect(coverPlan('Un', 'manga').family).toBe('manga');
    expect(coverKind('Manhwa')).toBe('manga');
    expect(coverKind('webtoon')).toBe('webtoon');
    expect(coverKind('BD')).toBe('comic');
    expect(coverKind(null)).toBe('book');
    const { container } = render(Cover, { title: 'Vingt mille lieues', author: 'Jules Verne' });
    const cover = container.querySelector('.cover')!;
    expect(cover).toHaveAttribute('aria-hidden', 'true'); // the title is written next to it
    expect(cover.querySelector('img')).toBeNull();
    expect(cover.textContent).toContain('Vingt mille lieues');
  });

  // req: EF-UX-008
  it('picks a scene of its family from the identifier of the series, the same one every time', () => {
    const ids = ['01JA0000000000000000000001', '01JA0000000000000000000002', '01JA0000000000000000000003'];
    for (const kind of ['manga', 'webtoon', 'comic'] as const) {
      const scenes = new Set(ids.map((id) => coverPlan(id, kind).scene?.name));
      expect(scenes.has(undefined)).toBe(false); // a drawn family always has its scene
      expect(coverPlan(ids[0]!, kind)).toEqual(coverPlan(ids[0]!, kind));
    }
    expect(coverPlan(ids[0]!, 'book').scene).toBeNull(); // a bound novel has its motif instead
    // the identifier decides, not the title: two volumes of one title can differ
    const { container } = render(Cover, { title: 'Little Nemo', seed: ids[0], kind: 'comic', author: 'Winsor McCay' });
    const cover = container.querySelector('.cover')!;
    expect(cover.className).toContain(`scene-${coverPlan(ids[0]!, 'comic').scene!.name}`);
    expect(cover.querySelector('.cover__panel svg path, .cover__panel svg rect')).not.toBeNull();
    expect(cover.querySelector('.cover__title')).toHaveTextContent('Little Nemo');
    expect(cover.querySelector('[style]')).toBeNull(); // no inline style (CSP)
  });

  // req: EF-UX-008
  it('a real image covers the drawn cover, which stays under it until the image has loaded', () => {
    const { container } = render(Cover, { title: 'Hokusai Manga', seed: '01JA0000000000000000000001', kind: 'manga', src: '/api/v1/thumbnails/abc/320' });
    const cover = container.querySelector('.cover')!;
    expect(cover.querySelector('img')).toHaveAttribute('src', '/api/v1/thumbnails/abc/320');
    // the composed cover is under the picture: no empty slot while it loads or scrolls into view
    expect(cover.querySelector('.cover__art')).not.toBeNull();
    expect(cover.querySelector('.cover__title')).toHaveTextContent('Hokusai Manga');
    expect(cover).toHaveAttribute('data-image', 'pending');
    fireEvent.load(cover.querySelector('img')!);
    expect(cover).toHaveAttribute('data-image', 'loaded');
  });

  // req: EF-UX-008
  it('shows the real image, and falls back to the drawn cover when it fails', async () => {
    const { container } = render(Cover, { title: 'Nemo', src: '/api/v1/thumbnails/abc/320' });
    const img = container.querySelector('img')!;
    expect(img).toHaveAttribute('alt', '');
    expect(img).toHaveAttribute('src', '/api/v1/thumbnails/abc/320');
    await fireEvent.error(img);
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('Nemo');
  });

  // req: EF-UX-005
  it('a locked cover never prints the title of the work', () => {
    const { container } = render(Cover, { title: 'Titre secret', locked: true });
    expect(container.textContent).not.toContain('Titre secret');
    expect(container.textContent).toContain('Œuvre réservée');
  });

  // req: ENF-PRIV-001
  it('the line drawings are inline SVG, decorative', () => {
    const { container } = render(Illustration, { name: 'missing' });
    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    expect(container.innerHTML).not.toMatch(/https?:/);
  });
});
