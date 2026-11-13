import { render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { describe, expect, it } from 'vitest';
import Layout from '../../routes/+layout.svelte';
import Footer from './Footer.svelte';

const SOURCE = 'https://github.com/Themlaw/shelfhold/tree/v1.0.0';

describe('footer with the source link (AGPL section 13)', () => {
  // req: ENF-LEG-012
  it('test_ui_footer_has_source_link', () => {
    render(Footer, { sourceUrl: SOURCE });
    const link = screen.getByRole('link', { name: 'Code source' });
    expect(link).toHaveAttribute('href', SOURCE);
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));
    expect(screen.getByRole('contentinfo')).toContainElement(link);
  });

  // req: ENF-LEG-012
  it('the root layout renders the footer under every screen', () => {
    const page = createRawSnippet(() => ({ render: () => '<main><h1>Un écran</h1></main>' }));
    render(Layout, { children: page, data: { sourceUrl: SOURCE, appearance: null, nav: [], language: 'fr' } } as never);
    expect(screen.getByRole('heading', { name: 'Un écran' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Code source' })).toHaveAttribute('href', SOURCE);
  });

  // req: ENF-LEG-012
  it('the root layout still renders the footer when the page is empty', () => {
    const page = createRawSnippet(() => ({ render: () => '<span></span>' }));
    render(Layout, { children: page, data: { sourceUrl: SOURCE, appearance: null, nav: [], language: 'fr' } } as never);
    expect(screen.getByRole('contentinfo')).toBeInTheDocument();
  });

  // req: ENF-LEG-012, ENF-SEC-004
  it.each([
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    ' javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'vbscript:x',
    '//evil.example/x',
    '/relative',
    ''
  ])('never turns %j into a link', (url) => {
    render(Footer, { sourceUrl: url });
    for (const link of screen.queryAllByRole('link')) {
      expect(link.getAttribute('href') ?? '').toMatch(/^https?:\/\//);
    }
    expect(screen.queryByRole('link', { name: 'Code source' })).toBeNull();
  });

  // req: ENF-LEG-012
  it('says so, rather than hiding it, when the address is not known', () => {
    render(Footer, { sourceUrl: null });
    expect(screen.getByRole('contentinfo')).toHaveTextContent('Code source');
  });

  // req: ENF-LEG-012
  it('renders the address as text, never as markup', () => {
    render(Footer, { sourceUrl: 'https://example.org/<img src=x onerror=alert(1)>' });
    expect(document.querySelector('img')).toBeNull();
  });
});
