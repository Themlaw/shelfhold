// The sign-in page is written by the backend; its script is a file of the backend. This runs that
// very file against the page's own markup, with a fetch that records what the form sends.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import { describe, expect, it } from 'vitest';

const pages = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'backend', 'src', 'shelfhold', 'interfaces', 'http_api', 'pages');
const read = (name: string) => readFileSync(join(pages, name), 'utf8');

type Sent = { url: string; body: Record<string, unknown> };

/** The page as the server fills it (every placeholder with a neutral word), its script loaded. */
function loginPage(answer: { ok: boolean; body?: unknown } = { ok: true }) {
  const html = read('login.html')
    .replace(/<script src[^>]*><\/script>/g, '')
    .replace(/\$max_password/g, '1024')
    .replace(/\$endpoint/g, '/api/v1/auth/login')
    .replace(/\$[a-z_]+/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'https://library.test/login' });
  const sent: Sent[] = [];
  const window = dom.window as unknown as Record<string, unknown>;
  window.fetch = async (url: string, init: { body: string }) => {
    sent.push({ url, body: JSON.parse(init.body) });
    return { ok: answer.ok, json: async () => answer.body ?? {} };
  };
  dom.window.eval(read('login.js'));
  const form = dom.window.document.getElementById('login') as HTMLFormElement;
  const submit = () => form.dispatchEvent(new dom.window.Event('submit', { cancelable: true, bubbles: true }));
  return { dom, form, sent, submit, document: dom.window.document };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('the sign-in page script', () => {
  // req: EF-USR-022
  it('a box the person left ticked is the explicit request the hardened profile asks for', async () => {
    const page = loginPage();
    (page.document.getElementById('username') as HTMLInputElement).value = 'alice';
    (page.document.getElementById('password') as HTMLInputElement).value = 'secret';
    page.submit();
    await settle();
    expect(page.sent).toHaveLength(1);
    expect(page.sent[0]?.body).toMatchObject({ username: 'alice', remember: true, confirm_remember: true });
  });

  // req: EF-USR-022
  it('a box left clear asks for a browser session and confirms nothing', async () => {
    const page = loginPage();
    (page.document.querySelector('input[name=remember]') as HTMLInputElement).checked = false;
    page.submit();
    await settle();
    expect(page.sent[0]?.body).toMatchObject({ remember: false, confirm_remember: false });
  });

  // req: EF-USR-021
  it('says the title of the refusal and the way out, and gives the button back', async () => {
    const page = loginPage({ ok: false, body: { title: 'Refused', action: 'Try again.' } });
    page.submit();
    await settle();
    const message = page.document.getElementById('message') as HTMLElement;
    expect(message.hidden).toBe(false);
    expect(message.textContent).toContain('Refused');
    expect(message.textContent).toContain('Try again.');
    expect(page.document.querySelector('button[type=submit]')?.hasAttribute('aria-disabled')).toBe(false);
  });

  // req: EF-USR-021
  it('sends the form only once while it waits for the answer', async () => {
    const page = loginPage();
    page.submit();
    page.submit();
    await settle();
    expect(page.sent).toHaveLength(1);
  });
});
