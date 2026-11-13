import type { Page } from '@playwright/test';
import {
  expect,
  expectNoHorizontalScroll,
  expectScreenAccessible,
  signInAs,
  test
} from './fixtures';

// Workshop accessibility (ENF-A11Y-001..004): every workshop screen, on desktop and on 360x780.

const SCREENS = [
  '/',
  '/a-classer',
  '/a-ranger',
  '/a-approuver',
  '/mes-demandes',
  '/activite',
  '/bibliotheque',
  '/assistant/compte',
  '/assistant/dossier',
  '/assistant/etageres',
  '/assistant/fin',
  '/assistant/kavita',
  '/compte/passkeys',
  '/reglages',
  '/reglages/apparence',
  '/reglages/notifications',
  '/reglages/profils'
];

const MAX_STOPS = 300;

/** Every workshop screen, with the sheet of the first series of the library: no screen is exempt. */
async function allScreens(page: Page): Promise<string[]> {
  await page.goto('/bibliotheque');
  await settle(page);
  const sheet = await page.locator('a[href^="/serie/"]').first().getAttribute('href');
  expect(sheet, 'the synthetic library has a series with a sheet').not.toBeNull();
  return [...SCREENS, sheet as string];
}

/** The screens hold an event stream open, so the network never goes idle: wait for the screen itself. */
async function settle(page: Page): Promise<void> {
  await page.getByRole('heading', { level: 1 }).first().waitFor();
  await page.waitForTimeout(300);
}

type Stop = { tag: string; name: string; visible: boolean; ring: boolean; first: boolean };

// req: ENF-A11Y-002
test('test_keyboard_navigation_workshop', async ({ page }) => {
  test.setTimeout(120_000);
  await signInAs(page, 'admin');
  for (const path of await allScreens(page)) {
    await page.goto(path);
    await settle(page);
    // Every control that is shown can be reached: none is taken out of the tab order.
    const unreachable = await page.evaluate(() =>
      Array.from(
        document.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), summary')
      )
        .filter((el) => el.getClientRects().length > 0 && el.tabIndex < 0)
        .map((el) => el.tagName + (el.getAttribute('name') ? `[${el.getAttribute('name')}]` : ''))
    );
    expect(unreachable, `${path}: controls out of the tab order`).toEqual([]);

    const stops: Stop[] = [];
    for (let i = 0; i < MAX_STOPS; i += 1) {
      await page.keyboard.press('Tab');
      const stop = await page.evaluate((): Stop | null => {
        const el = document.activeElement as HTMLElement | null;
        if (!el || el === document.body) return null;
        const w = window as unknown as { __firstStop?: Element };
        w.__firstStop ??= el;
        const rect = el.getBoundingClientRect();
        const own = getComputedStyle(el);
        const host = el.closest('label') ?? el;
        const hostStyle = getComputedStyle(host);
        const outlined = (st: CSSStyleDeclaration) => st.outlineStyle !== 'none' && parseFloat(st.outlineWidth) > 0;
        return {
          first: w.__firstStop === el,
          tag: el.tagName,
          name: el.id + ' ' + (el.getAttribute('type') ?? '') + ' ' + (el.getAttribute('aria-label') ?? el.textContent?.trim().slice(0, 30) ?? ''),
          visible: rect.width > 0 && rect.height > 0 && own.visibility !== 'hidden',
          // the ring is on the control, or on the label that wraps a control hidden by its look
          ring: outlined(own) || outlined(hostStyle) || own.boxShadow !== 'none'
        };
      });
      if (stop === null) break; // the focus left the page: the cycle is complete
      if (stops.length > 0 && stop.first) break; // back on the first stop: the cycle is complete
      stops.push(stop);
    }
    expect(stops.length, `${path}: no Tab stop at all`).toBeGreaterThan(0);
    for (const stop of stops) {
      expect(stop.visible, `${path}: focus on an invisible ${stop.tag} ${stop.name}`).toBe(true);
      expect(stop.ring, `${path}: no visible focus ring on ${stop.tag} ${stop.name}`).toBe(true);
    }
    expect(stops.length, `${path}: keyboard trap`).toBeLessThan(MAX_STOPS);
  }
});

// req: ENF-A11Y-003
test('test_reduced_motion', async ({ page }) => {
  test.setTimeout(120_000);
  await signInAs(page, 'admin');
  const probe = () =>
    page.evaluate(() => {
      // a probe that asks for motion the way a component would: the answer shows who wins
      const el = document.createElement('div');
      el.style.transition = 'all 2s';
      el.style.animation = 'probe-spin 2s infinite';
      document.body.append(el);
      document.documentElement.style.scrollBehavior = 'smooth';
      const style = getComputedStyle(el);
      const answer = {
        transition: parseFloat(style.transitionDuration),
        animation: parseFloat(style.animationDuration),
        iterations: style.animationIterationCount,
        scroll: getComputedStyle(document.documentElement).scrollBehavior,
        moving: Array.from(document.querySelectorAll('*'))
          .filter((n) => {
            const s = getComputedStyle(n);
            return n !== el && (parseFloat(s.transitionDuration) > 0.001 || parseFloat(s.animationDuration) > 0.001);
          })
          .map((n) => n.tagName)
      };
      el.remove();
      document.documentElement.style.scrollBehavior = '';
      return answer;
    });
  for (const path of await allScreens(page)) {
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await page.goto(path);
    await settle(page);
    // the control: without the preference the probe keeps its motion, so the test can tell
    const free = await probe();
    expect(free.transition, `${path}: control`).toBeGreaterThan(1);

    await page.emulateMedia({ reducedMotion: 'reduce' });
    const reduced = await probe();
    expect(reduced.transition, `${path}: transition`).toBeLessThanOrEqual(0.001);
    expect(reduced.animation, `${path}: animation`).toBeLessThanOrEqual(0.001);
    expect(reduced.iterations, `${path}: iterations`).toBe('1');
    expect(reduced.scroll, `${path}: scroll`).toBe('auto');
    expect(reduced.moving, `${path}: elements still in motion`).toEqual([]);
  }
});

// req: ENF-A11Y-004
test('test_touch_targets_44px_and_cards_under_600px', async ({ page }) => {
  test.setTimeout(120_000);
  await signInAs(page, 'admin');
  const narrow = (page.viewportSize()?.width ?? 1280) < 600;
  for (const path of await allScreens(page)) {
    await page.goto(path);
    await settle(page);
    const tooSmall = await page.evaluate(() => {
      const small: string[] = [];
      const nodes = document.querySelectorAll<HTMLElement>(
        'button, a[href], input:not([type="hidden"]), select, textarea, summary, [role="button"], [role="link"]'
      );
      for (const el of nodes) {
        if (el.getClientRects().length === 0) continue;
        const style = getComputedStyle(el);
        if (style.visibility === 'hidden') continue;
        // WCAG 2.5.8: a link inside a sentence is exempt, a link on its own is not
        if (el.tagName === 'A') {
          const around = Array.from(el.parentElement?.childNodes ?? []).filter(
            (n) => n !== el && n.nodeType === Node.TEXT_NODE && (n.textContent ?? '').trim() !== ''
          );
          if (around.length > 0) continue;
        }
        // a checkbox or a radio is reached through the label that wraps it
        const isChoice = el instanceof HTMLInputElement && (el.type === 'checkbox' || el.type === 'radio');
        const target = isChoice ? (el.closest('label') ?? el) : el;
        const rect = target.getBoundingClientRect();
        if (rect.width < 43.5 || rect.height < 43.5) {
          small.push(`${el.tagName} ${el.getAttribute('name') ?? el.textContent?.trim().slice(0, 20) ?? ''} ${rect.width.toFixed(0)}x${rect.height.toFixed(0)}`);
        }
      }
      return small;
    });
    expect(tooSmall, `${path}: targets under 44x44 px`).toEqual([]);
    await expectNoHorizontalScroll(page);

    if (narrow) {
      const layout = await page.evaluate(() => ({
        // no table is laid out as a table: the rows are stacked cards
        tables: Array.from(document.querySelectorAll('table, tr, td')).filter((n) => {
          const d = getComputedStyle(n).display;
          return d.startsWith('table');
        }).length,
        // the main action of a form is stuck to the bottom of the screen
        loose: Array.from(document.querySelectorAll<HTMLElement>('form > button[type="submit"]:last-child, .main-action'))
          .filter((el) => el.getClientRects().length > 0)
          .filter((el) => getComputedStyle(el).position !== 'sticky')
          .map((el) => el.tagName)
      }));
      expect(layout.tables, `${path}: table layout under 600 px`).toBe(0);
      expect(layout.loose, `${path}: main action not sticky`).toEqual([]);
    }
  }
});

// req: ENF-A11Y-004
test('test_main_action_stays_in_reach_on_a_long_form', async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 1280) >= 600, 'the sticky action is a phone rule');
  await signInAs(page, 'admin');
  await page.goto('/reglages/profils');
  await settle(page);
  const create = page.locator('form > button[type="submit"]').first();
  await expect(create).toBeVisible();
  // pad the page so the form extends below the fold, then check the button is still on screen
  await page.evaluate(() => {
    const form = document.querySelector('form');
    const pad = document.createElement('div');
    pad.style.height = '3000px';
    form?.insertBefore(pad, form.querySelector('button[type="submit"]'));
  });
  await page.evaluate(() => window.scrollTo(0, 0));
  const box = await create.boundingBox();
  const height = page.viewportSize()?.height ?? 780;
  expect(box && box.y + box.height <= height, 'the main action is inside the first screen').toBe(true);
});

// req: ENF-A11Y-001
test('test_axe_blocking_every_workshop_screen', async ({ page }) => {
  test.setTimeout(240_000);
  await signInAs(page, 'admin');
  for (const path of await allScreens(page)) {
    await page.goto(path);
    await settle(page);
    await expect(page.getByRole('heading', { level: 1 }), `${path}: one h1`).toHaveCount(1);
    await expectScreenAccessible(page);
    await expectNoHorizontalScroll(page);
  }
});
