<script lang="ts">
  // The shell of every screen (« Papier et encre », docs/design/papier-et-encre.md): the
  // masthead, the table of contents on the left (the places of the role, EF-UX-005), the screen,
  // the colophon (the source link, ENF-LEG-012), and on a phone the tab bar with « More » that
  // opens the whole table. Nobody signed in: the masthead and the colophon only.
  import { onMount } from 'svelte';
  import { afterNavigate } from '$app/navigation';
  import { patchPreferences } from '#lib/api/index.ts';
  import { guardOfflineSession, startOfflineSync, wipeOfflineCopies } from '#lib/offline/runtime.ts';
  import { registerServiceWorker } from '#lib/pwa/register.ts';
  import {
    applyAppearance,
    loadAppearance,
    rememberAppearance,
    type ColorScheme
  } from '#lib/themes/index.ts';
  import { detectLanguage, setUiLanguage, t } from '#lib/i18n/index.ts';
  import { currentPlace, roleOf, tabBarPlaces, tableOfContents } from '#lib/nav.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { signOut } from '#lib/session.ts';
  import Dialog from '#lib/ui/molecules/Dialog.svelte';
  import Toast from '#lib/ui/molecules/Toast.svelte';
  import AppHeader, { type HeaderUser } from '#lib/ui/organisms/AppHeader.svelte';
  import Colophon from '#lib/ui/organisms/Colophon.svelte';
  import MobileTabBar from '#lib/ui/organisms/MobileTabBar.svelte';
  import TableOfContents, { type TocGroup } from '#lib/ui/organisms/TableOfContents.svelte';
  import { notify } from '#lib/ui/toast.svelte.ts';
  import '#lib/themes/base.css';
  import '#lib/themes/themes.css';
  import '#lib/themes/eink.css';
  import '#lib/themes/foundation.css';
  import './global.css';

  let { children, data } = $props();

  // The installable shell (EF-LEC-012): registered by the SPA, never by the reader, and only on a build
  // (the worker is a product of `vite build`).
  onMount(() => {
    if (import.meta.env.PROD) void registerServiceWorker(navigator, location.pathname);
  });

  // The offline copies belong to the profile signed in: another one's are wiped, what was read offline
  // is sent, what left the view is evicted (L16b). Nobody known (offline, signed out): nothing changes here.
  $effect(() => {
    // the server refused the session (revoked, expired, account off): nothing of it stays on the device
    if (data.sessionLost) void wipeOfflineCopies();
    const profile = data.profileId;
    if (!import.meta.env.PROD) return;
    // `/me` did not name anyone: another session than the one the copies were kept under is a wipe. When
    // it does, `startOfflineSync` activates that profile (wiping the others') and binds the session.
    if (!profile) {
      void guardOfflineSession(null);
      return;
    }
    return startOfflineSync(profile);
  });

  // The boot script of app.html already set the attributes from the last visit; the answer of the
  // server (another device may have changed it) applies here, once the screen has its data.
  let scheme = $state<ColorScheme>(typeof localStorage === 'undefined' ? 'auto' : loadAppearance(localStorage).colorScheme);
  $effect(() => {
    if (data.appearance) {
      applyAppearance(document.documentElement, data.appearance);
      scheme = data.appearance.colorScheme;
    }
    // a later answer (a sign-in, a changed preference) changes the language too
    setUiLanguage(data.language ?? detectLanguage(navigator.languages));
  });

  /** Light, dark or as the system: applied and remembered on this device at once, kept on the
   * profile when someone is signed in (the next device opens the same way, EF-UX-012). */
  async function chooseScheme(next: ColorScheme): Promise<void> {
    const appearance = { ...loadAppearance(localStorage), colorScheme: next };
    applyAppearance(document.documentElement, appearance);
    rememberAppearance(localStorage, appearance);
    scheme = next;
    if (!user) return;
    try {
      await patchPreferences({
        headers: { 'X-CSRF-Token': csrfToken() },
        body: { color_scheme: next },
        throwOnError: true
      });
    } catch {
      notify({ tone: 'warning', title: t('ui.scheme.not_saved') });
    }
  }

  async function leave(): Promise<void> {
    if (!(await signOut())) notify({ tone: 'danger', title: t('nav.logout_failed') });
  }

  // Who is signed in, and the places of their role.
  const role = $derived(roleOf(data.user?.role));
  const user = $derived<HeaderUser | null>(
    data.user && role
      ? { name: data.user.name, role: role === 'member' ? 'adult' : role }
      : null
  );
  const groups = $derived(tableOfContents(data.nav ?? [], role));
  const toc = $derived<TocGroup[]>(
    groups.map((group) => ({
      label: t(group.labelKey),
      entries: group.items.map((place) => ({ href: place.href, label: t(place.labelKey), icon: place.icon }))
    }))
  );
  const tabs = $derived(
    tabBarPlaces(data.nav ?? [], role).map((place) => ({ href: place.href, label: t(place.labelKey), icon: place.icon }))
  );
  const more = $derived(role !== 'child' && tabs.length > 0);

  // The address shown, for the entry marked as current (the router does not reload the layout).
  let pathname = $state(typeof location === 'undefined' ? '/' : location.pathname);
  afterNavigate((navigation) => {
    if (navigation?.to?.url) pathname = navigation.to.url.pathname;
    sheet = false;
  });
  const current = $derived.by(() => {
    const places = groups.flatMap((group) => group.items);
    const id = currentPlace(pathname, places);
    return places.find((place) => place.id === id)?.href ?? null;
  });

  let sheet = $state(false);
</script>

<a class="skip" href="#contenu">{t('ui.layout.skip')}</a>
<div class="app" class:app--toc={toc.length > 0}>
  <AppHeader {user} {scheme} needsAdmin={data.needsAdmin} onscheme={(next) => void chooseScheme(next)} onsignout={() => void leave()}>
    {#snippet notice()}<Toast />{/snippet}
  </AppHeader>
  {#if toc.length > 0}
    <!-- the column holds the rule over the whole height; the list inside stays in view -->
    <aside class="app__toc">
      <div class="app__toc-list">
        <TableOfContents groups={toc} {current} title={t('ui.toc.title')} label={t('nav.label')} />
      </div>
    </aside>
  {/if}
  <div class="app__screen" id="contenu" tabindex="-1">{@render children()}</div>
  <Colophon sourceUrl={data.sourceUrl} />
</div>
{#if tabs.length > 0}
  <MobileTabBar
    entries={tabs}
    {current}
    label={t('ui.tabbar.label')}
    moreOpen={sheet}
    onmore={more ? () => (sheet = true) : undefined}
    comfortable={role === 'child'}
  />
  {#if more}
    <Dialog bind:open={sheet} title={t('ui.toc.title')}>
      <TableOfContents groups={toc} {current} label={t('nav.label')} onnavigate={() => (sheet = false)} />
    </Dialog>
  {/if}
{/if}

<style>
  .skip {
    position: absolute;
    top: var(--space-2);
    left: var(--space-2);
    z-index: var(--z-toast);
    display: inline-flex;
    align-items: center;
    min-height: var(--target);
    padding: var(--space-2) var(--space-4);
    color: var(--color-on-accent);
    background: var(--color-accent);
    border-radius: var(--radius-sm);
    transform: translateY(-200%);
  }
  .skip:focus-visible {
    transform: none;
  }
  .app {
    display: grid;
    min-height: 100dvh;
    grid-template:
      'head' auto
      'main' 1fr
      'foot' auto / minmax(0, 1fr);
  }
  .app--toc {
    grid-template:
      'head head' auto
      'side main' 1fr
      'foot foot' auto / var(--sidebar) minmax(0, 1fr);
  }
  .app > :global(.masthead) {
    grid-area: head;
  }
  /* the column stretches over the row (its rule runs the whole height, and the row grows to the
     list: the list never runs into the colophon); the list sticks under the header */
  .app__toc {
    grid-area: side;
    min-width: 0;
    border-right: var(--rule-hair) solid var(--color-rule);
  }
  .app__toc-list {
    position: sticky;
    top: calc(var(--header-h) + var(--space-1));
    max-height: calc(100dvh - var(--header-h) - var(--space-1));
    overflow-y: auto;
    padding: var(--space-6) var(--space-4) var(--space-5) var(--space-5);
  }
  .app__screen {
    grid-area: main;
    min-width: 0;
    padding: var(--space-6) var(--space-7) var(--space-8);
  }
  .app__screen:focus {
    outline: none;
  }
  .app > :global(.colophon) {
    grid-area: foot;
  }
  /* the rule of the column meets the rule over the colophon */
  .app--toc > :global(.colophon) {
    margin-top: 0;
    border-top: var(--rule-hair) solid var(--color-rule);
  }
  @media (max-width: 75rem) {
    .app__screen {
      padding-inline: var(--space-5);
    }
  }
  @media (max-width: 60rem) {
    .app--toc {
      grid-template:
        'head' auto
        'main' 1fr
        'foot' auto / minmax(0, 1fr);
    }
    .app__toc {
      display: none;
    }
    .app__screen {
      padding: var(--space-5) var(--space-4) var(--space-7);
    }
    /* the end of the page is never under the tab bar nor under a notice docked on it */
    .app--toc > :global(.colophon) {
      padding-bottom: calc(var(--tabbar-h) + var(--notice-h) + var(--space-4));
    }
  }
</style>
