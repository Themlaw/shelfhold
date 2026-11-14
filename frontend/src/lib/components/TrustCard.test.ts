import { render, screen, within } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import TrustCard from './TrustCard.svelte';

// The fiche de confiance of a source (EF-UX-011): three lines, Officiel / Communauté / Inconnu,
// the server says which one the source has and what each line means. The card decides nothing.
const card = (current: 'official' | 'community' | 'unknown') => ({
  level: current,
  lines: [
    { level: 'official', label: 'Maintenue par le projet', current: current === 'official' },
    { level: 'community', label: 'Écrite par des bénévoles', current: current === 'community' },
    { level: 'unknown', label: 'Jamais examinée', current: current === 'unknown' }
  ]
});

describe('TrustCard', () => {
  // req: EF-UX-011
  it('test_module_trust_card_three_lines', () => {
    render(TrustCard, { trust: card('community') } as never);
    const list = screen.getByRole('list', { name: 'Niveau de confiance' });
    const lines = within(list).getAllByRole('listitem');
    expect(lines).toHaveLength(3);
    expect(lines[0]).toHaveTextContent('Officiel');
    expect(lines[0]).toHaveTextContent('Maintenue par le projet');
    expect(lines[1]).toHaveTextContent('Communauté');
    expect(lines[1]).toHaveTextContent('Écrite par des bénévoles');
    expect(lines[2]).toHaveTextContent('Inconnu');
    expect(lines[2]).toHaveTextContent('Jamais examinée');
  });

  // req: EF-UX-011
  it.each([
    ['official', 0],
    ['community', 1],
    ['unknown', 2]
  ] as const)('marks the line the server says is current (%s), by text and not by colour alone', (level, index) => {
    render(TrustCard, { trust: card(level) } as never);
    const lines = screen.getAllByRole('listitem');
    lines.forEach((line, position) => {
      if (position === index) {
        expect(line).toHaveAttribute('aria-current', 'true');
        expect(line).toHaveTextContent('Niveau actuel');
      } else {
        expect(line).not.toHaveAttribute('aria-current');
        expect(line).not.toHaveTextContent('Niveau actuel');
      }
    });
  });

  // req: EF-UX-011
  it('keeps the order and the text the server sent, even when it is not the usual order', () => {
    const odd = {
      level: 'unknown',
      lines: [
        { level: 'unknown', label: 'Texte A', current: true },
        { level: 'official', label: 'Texte B', current: false },
        { level: 'community', label: 'Texte C', current: false }
      ]
    };
    render(TrustCard, { trust: odd } as never);
    const lines = screen.getAllByRole('listitem');
    expect(lines.map((line) => line.textContent)).toEqual([
      expect.stringContaining('Texte A'),
      expect.stringContaining('Texte B'),
      expect.stringContaining('Texte C')
    ]);
    expect(lines[0]).toHaveAttribute('aria-current', 'true');
    expect(lines[0]).toHaveTextContent('Inconnu');
  });

  // req: EF-UX-011
  it('says honestly that there is no card, and makes up no level', () => {
    render(TrustCard, { trust: null } as never);
    expect(screen.queryByRole('list')).toBeNull();
    expect(screen.getByText('Fiche de confiance indisponible')).toBeInTheDocument();
    expect(screen.queryByText(/Officiel|Communauté/)).toBeNull();
  });

  // req: EF-UX-011
  it('shows a label as text, never as markup', () => {
    const hostile = card('official');
    hostile.lines[0]!.label = '<img src=x onerror=alert(1)>';
    const { container } = render(TrustCard, { trust: hostile } as never);
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getAllByRole('listitem')[0]).toHaveTextContent('<img src=x onerror=alert(1)>');
  });
});
