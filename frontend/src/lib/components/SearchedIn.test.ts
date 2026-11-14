import { render, screen, within } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import SearchedIn from './SearchedIn.svelte';

// "Recherché dans…" (EF-UX-011): the sources that were asked, with what each one returned, the
// ones that did not answer, and each one's trust card. Never an address, never an error code.
const trust = {
  level: 'official',
  lines: [
    { level: 'official', label: 'Maintenue par le projet', current: true },
    { level: 'community', label: 'Écrite par des bénévoles', current: false },
    { level: 'unknown', label: 'Jamais examinée', current: false }
  ]
};
const source = (key: string, name: string, over: Record<string, unknown> = {}) => ({
  source_key: key,
  name,
  format: 'text',
  status: 'ok',
  code: null,
  returned: 3,
  trust,
  ...over
});

describe('SearchedIn', () => {
  // req: EF-UX-011
  it('test_searched_in_lists_sources', () => {
    render(SearchedIn, {
      sources: [
        source('alpha', 'Alpha texts', { returned: 3 }),
        source('beta', 'Beta scans', { returned: 1, format: 'images' }),
        source('gamma', 'Gamma wiki', { returned: 0, trust: null })
      ]
    } as never);
    const section = screen.getByRole('region', { name: 'Recherché dans' });
    expect(within(section).getByRole('heading', { name: 'Recherché dans' })).toBeInTheDocument();
    const rows = within(section).getAllByTestId('searched-source');
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent('Alpha texts');
    expect(rows[0]).toHaveTextContent('3 résultats');
    expect(rows[1]).toHaveTextContent('Beta scans');
    expect(rows[1]).toHaveTextContent('1 résultat');
    expect(rows[1]).not.toHaveTextContent('1 résultats');
    expect(rows[2]).toHaveTextContent('Gamma wiki');
    expect(rows[2]).toHaveTextContent('aucun résultat');
  });

  // req: EF-UX-011
  it('says which source did not answer, in words, and never shows the code or an address', () => {
    render(SearchedIn, {
      sources: [
        source('alpha', 'Alpha texts'),
        source('beta', 'Beta scans', {
          status: 'error',
          code: 'source.timeout',
          returned: 0,
          trust: null
        })
      ]
    } as never);
    const [ok, failed] = screen.getAllByTestId('searched-source');
    expect(failed).toHaveTextContent('Beta scans');
    expect(failed).toHaveTextContent('Indisponible');
    expect(ok).not.toHaveTextContent('Indisponible');
    expect(document.body.textContent).not.toMatch(/source\.timeout|https?:\/\//);
    // an error is not "0 results": the two are not told the same way
    expect(failed).not.toHaveTextContent('aucun résultat');
  });

  // req: EF-UX-011
  it('gives each source its own trust card, and none to a source that has none', () => {
    render(SearchedIn, {
      sources: [source('alpha', 'Alpha texts'), source('beta', 'Beta scans', { trust: null })]
    } as never);
    const [first, second] = screen.getAllByTestId('searched-source');
    expect(within(first!).getByRole('list', { name: 'Niveau de confiance' })).toBeInTheDocument();
    expect(within(second!).queryByRole('list', { name: 'Niveau de confiance' })).toBeNull();
    expect(within(second!).getByText('Fiche de confiance indisponible')).toBeInTheDocument();
  });

  // req: EF-UX-011
  it('renders nothing at all when no source was asked', () => {
    const { container } = render(SearchedIn, { sources: [] } as never);
    expect(screen.queryByRole('region')).toBeNull();
    expect(container.textContent?.trim()).toBe('');
  });

  // req: EF-UX-011
  it('writes a source name as text, never as markup', () => {
    const { container } = render(SearchedIn, {
      sources: [source('alpha', '<b>Gras</b><img src=x>')]
    } as never);
    expect(container.querySelector('b, img')).toBeNull();
    expect(screen.getByTestId('searched-source')).toHaveTextContent('<b>Gras</b><img src=x>');
  });
});
