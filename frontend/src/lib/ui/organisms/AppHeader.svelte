<script lang="ts" module>
  export type HeaderUser = { name: string; role: 'admin' | 'adult' | 'child' };
  export type Scheme = 'auto' | 'light' | 'dark';
</script>

<script lang="ts">
  // The masthead of every screen: the mark and the name of the house, the search of the
  // catalogue (a link on a phone), the notice of the moment (Toast, given by the layout), the
  // scheme (light, dark, as the system) and the account menu. Nobody signed in: a link to the
  // sign-in page of the server (a full load: it is not a screen of this application).
  import type { Snippet } from 'svelte';
  import { t } from '#lib/i18n/index.ts';
  import Avatar from '../atoms/Avatar.svelte';
  import Icon from '../atoms/Icon.svelte';
  import { MARK } from '../atoms/icons.ts';
  import Shapes from '../atoms/Shapes.svelte';
  import Menu, { type MenuItem } from '../molecules/Menu.svelte';
  import SearchBar from '../molecules/SearchBar.svelte';

  let {
    user = null,
    scheme = 'auto',
    searchHref = '/chercher',
    signInHref = '/login',
    notice,
    onscheme,
    onsignout
  }: {
    user?: HeaderUser | null;
    scheme?: Scheme;
    searchHref?: string;
    signInHref?: string;
    /** the notice of the moment, docked in the masthead on a wide screen */
    notice?: Snippet;
    onscheme?: (scheme: Scheme) => void;
    onsignout?: () => void;
  } = $props();

  let chosen = $derived<string>(scheme);

  const roleLabel = (role: HeaderUser['role']) =>
    role === 'admin' ? t('ui.role.admin') : role === 'child' ? t('ui.role.child') : t('ui.role.adult');

  const schemeIcon = $derived(scheme === 'dark' ? 'moon' : scheme === 'light' ? 'sun' : 'device');

  const accountItems = $derived<MenuItem[]>([
    { label: t('nav.passkeys'), icon: 'key', href: '/compte/passkeys' },
    { label: t('nav.trackers'), icon: 'shield', href: '/compte/trackers' },
    { kind: 'separator' },
    { label: t('ui.account.sign_out'), icon: 'logout', onselect: () => onsignout?.() }
  ]);
</script>

<header class="masthead">
  <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
  <a class="brand" href="/">
    <svg class="brand__mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false"><Shapes shapes={MARK} /></svg>
    <span class="brand__text">
      <span class="brand__name">Shelfhold</span>
      <span class="brand__house">
        {user?.role === 'child' ? t('ui.header.house_child', { name: user.name }) : t('ui.header.house')}
      </span>
    </span>
  </a>

  {#if user}
    <SearchBar
      class="masthead__search"
      action={searchHref}
      label={t('ui.header.search_label')}
      placeholder={user.role === 'child' ? t('ui.header.search_placeholder_child') : t('ui.header.search_placeholder')}
      shortcut
    />
  {/if}

  {#if notice}<div class="masthead__notice">{@render notice()}</div>{/if}

  <div class="masthead__tools">
    {#if user}
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
      <a class="tool masthead__mobile" href={searchHref} aria-label={t('ui.header.search_label')}>
        <Icon name="search" />
      </a>
    {/if}
    <Menu
      choices={[
        { value: 'light', label: t('ui.scheme.light'), icon: 'sun' },
        { value: 'dark', label: t('ui.scheme.dark'), icon: 'moon' },
        { value: 'auto', label: t('ui.scheme.auto'), icon: 'device' }
      ]}
      bind:value={chosen}
      choicesLabel={t('ui.scheme.label')}
      onchoose={(value) => onscheme?.(value as Scheme)}
    >
      {#snippet trigger({ props })}
        <button {...props} type="button" class="tool" aria-label={t('ui.scheme.label')} title={t('ui.scheme.label')}>
          <Icon name={schemeIcon} />
        </button>
      {/snippet}
    </Menu>
    {#if user}
      <Menu items={accountItems}>
        {#snippet trigger({ props })}
          <button
            {...props}
            type="button"
            class="me"
            aria-label={t('ui.account.menu', { name: user.name, role: roleLabel(user.role) })}
          >
            <span class="me__name" aria-hidden="true"><strong>{user.name}</strong><small>{roleLabel(user.role)}</small></span>
            <Avatar name={user.name} kind={user.role} size="sm" />
          </button>
        {/snippet}
      </Menu>
    {:else}
      <!-- the sign-in page belongs to the server: a full load, never the router -->
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- a page of the server -->
      <a class="signin" href={signInHref} data-sveltekit-reload>
        <Icon name="key" /><span>{t('ui.account.sign_in')}</span>
      </a>
    {/if}
  </div>
</header>

<style>
  .masthead {
    position: sticky;
    top: 0;
    z-index: var(--z-sticky);
    display: flex;
    align-items: center;
    gap: var(--space-5);
    min-height: var(--header-h);
    padding: var(--space-2) var(--space-5);
    background: var(--color-surface-chrome);
    /* the double rule of a title page: a hairline, the chrome, a hairline */
    border-bottom: var(--rule-hair) solid var(--color-ink);
    box-shadow:
      0 var(--space-1) 0 calc(-1 * var(--rule-hair)) var(--color-surface-chrome),
      0 calc(var(--space-1) + var(--rule-hair)) 0 calc(-1 * var(--rule-hair)) var(--color-ink);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    min-width: 0;
    min-height: var(--target);
    color: var(--color-ink);
    text-decoration: none;
  }
  .brand__mark {
    flex: none;
    width: 2rem;
    height: 2rem;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.4;
    stroke-linejoin: round;
  }
  .brand__text {
    display: grid;
    min-width: 0;
  }
  .brand__name {
    font: 600 var(--step-2) / 1 var(--font-text);
    letter-spacing: -0.01em;
  }
  .brand__house {
    overflow: hidden;
    font: italic var(--step--1) / 1.2 var(--font-text);
    color: var(--color-muted);
    white-space: nowrap;
    text-overflow: ellipsis;
  }
  .masthead :global(.masthead__search) {
    flex: 1 1 auto;
    max-width: 34rem;
    margin-left: var(--space-6);
  }
  .masthead__notice {
    margin-left: auto;
    min-width: 0;
  }
  .masthead__tools {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-left: auto;
  }
  .masthead__notice + .masthead__tools {
    margin-left: 0;
  }
  .tool {
    display: inline-grid;
    place-items: center;
    width: var(--target);
    height: var(--target);
    padding: 0;
    color: var(--color-ink);
    background: none;
    border: 0;
    border-radius: var(--radius-sm);
    cursor: pointer;
  }
  .tool:hover {
    background: var(--color-surface);
  }
  .masthead__mobile {
    display: none;
  }
  .me {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    min-height: var(--target);
    padding: var(--space-1) var(--space-2) var(--space-1) var(--space-3);
    font: inherit;
    color: var(--color-ink);
    background: none;
    border: 0;
    border-radius: var(--radius-pill);
    cursor: pointer;
  }
  .me:hover,
  .me[aria-expanded='true'] {
    background: var(--color-surface);
  }
  .me__name {
    display: grid;
    line-height: 1.15;
    text-align: right;
  }
  .me__name small {
    font-family: var(--font-text);
    font-size: var(--step--1);
    font-style: italic;
    color: var(--color-muted);
  }
  .signin {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    min-height: var(--target);
    padding: 0 var(--space-3);
    font-weight: 500;
    color: var(--color-accent-text);
    white-space: nowrap;
  }
  @media (max-width: 75rem) {
    .me__name {
      display: none;
    }
  }
  @media (max-width: 60rem) {
    .masthead {
      gap: var(--space-3);
      padding: 0 var(--space-3) 0 var(--space-4);
    }
    .masthead :global(.masthead__search) {
      display: none;
    }
    .masthead__mobile {
      display: inline-grid;
    }
    .brand__name {
      font-size: var(--step-1);
    }
    .brand__house {
      max-width: 10rem;
    }
  }
  @media (max-width: 26rem) {
    .brand__house {
      max-width: 7rem;
    }
    .masthead__notice {
      display: contents;
    }
  }
</style>
