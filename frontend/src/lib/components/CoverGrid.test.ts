import { render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import CoverGrid from './CoverGrid.svelte';

const hash = (n: number) => n.toString(16).padStart(64, '0');
const THUMB = /^\/api\/v1\/thumbnails\/[0-9a-f]{64}\/(160|320)$/;

afterEach(() => document.documentElement.style.removeProperty('--variant-cover-layout'));

describe('CoverGrid', () => {
  // req: EF-BIB-020, ENF-PERF-004
  it('asks for thumbnails only: 60 covers, every src and srcset is a 160 or 320 thumbnail', () => {
    const items = Array.from({ length: 60 }, (_, i) => ({ id: `W${i}`, title: `Titre ${i}`, coverHash: hash(i) }));
    const { container } = render(CoverGrid, { items, label: 'Couvertures' });
    const images = [...container.querySelectorAll('img')];
    expect(images).toHaveLength(60);
    for (const img of images) {
      expect(img.getAttribute('src')).toMatch(THUMB);
      expect(img.getAttribute('src')).toMatch(/\/160$/);
      for (const part of (img.getAttribute('srcset') ?? '').split(',')) {
        expect(part.trim().split(' ')[0]).toMatch(THUMB);
      }
      expect(img.getAttribute('loading')).toBe('lazy');
      expect(img.getAttribute('width')).toBe('160');
      expect(img.getAttribute('height')).toBe('240');
    }
    expect(screen.getByAltText('Titre 7')).toBeInTheDocument();
  });

  // req: EF-BIB-020
  it('shows an empty slot, never an image, for a missing or malformed cover address', () => {
    const items = [
      { id: 'A', title: 'Sans couverture', coverHash: null },
      { id: 'B', title: 'Lien suspect', coverHash: '../../covers/original.webp' }
    ];
    const { container } = render(CoverGrid, { items, label: 'Couvertures' });
    expect(container.querySelectorAll('img')).toHaveLength(0);
    expect(screen.getByText('Sans couverture')).toBeInTheDocument();
    expect(screen.getByText('Lien suspect')).toBeInTheDocument();
  });

  // req: EF-UX-012
  it('follows the theme variant cover_layout, grid by default', () => {
    const items = [{ id: 'A', title: 'Un', coverHash: null }];
    const grid = render(CoverGrid, { items, label: 'Couvertures' });
    expect(grid.getByRole('list')).toHaveAttribute('data-layout', 'grid');
    grid.unmount();
    document.documentElement.style.setProperty('--variant-cover-layout', 'list');
    const list = render(CoverGrid, { items, label: 'Couvertures' });
    expect(list.getByRole('list')).toHaveAttribute('data-layout', 'list');
  });

  // req: EF-USR-006
  it('links to a sheet only where the caller gave a link', () => {
    render(CoverGrid, {
      items: [
        { id: 'A', title: 'Avec lien', coverHash: null, href: '/serie/E1' },
        { id: 'B', title: 'Sans lien', coverHash: null }
      ],
      label: 'Couvertures'
    });
    expect(screen.getByRole('link', { name: 'Avec lien' })).toHaveAttribute('href', '/serie/E1');
    expect(screen.queryByRole('link', { name: 'Sans lien' })).toBeNull();
  });
});
