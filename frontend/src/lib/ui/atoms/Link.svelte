<script lang="ts">
  // A link of the house. `quiet` is ink instead of blue (inside a list of titles). `external`
  // opens another site: it says so with an icon and a hidden word, and never sends the referrer.
  import type { Snippet } from 'svelte';
  import type { HTMLAnchorAttributes } from 'svelte/elements';
  import { t } from '#lib/i18n/index.ts';
  import Icon from './Icon.svelte';

  let {
    href,
    quiet = false,
    external = false,
    standalone = false,
    children,
    class: className = '',
    ...rest
  }: {
    href: string;
    quiet?: boolean;
    external?: boolean;
    standalone?: boolean;
    children: Snippet;
  } & Omit<HTMLAnchorAttributes, 'href' | 'children'> = $props();
</script>

<!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
<a {href}
  {...rest}
  class="link {className}"
  class:link--quiet={quiet}
  class:standalone
  rel={external ? 'noopener noreferrer' : rest.rel}
  target={external ? '_blank' : rest.target}
>
  {@render children()}
  {#if external}<Icon name="external" size="sm" /><span class="visually-hidden">{t('ui.link.new_window')}</span>{/if}
</a>

<style>
  .link {
    display: inline-flex;
    align-items: baseline;
    gap: var(--space-1);
    color: var(--color-accent-text);
    font-weight: 500;
  }
  .link :global(.icon) {
    align-self: center;
  }
  .link--quiet {
    color: var(--color-ink);
    text-decoration-color: var(--color-rule-strong);
  }
  .standalone {
    align-items: center;
    min-height: var(--target);
  }
</style>
