<script lang="ts" module>
  export type ErrorKind = 'not-found' | 'forbidden' | 'signed-out' | 'server' | 'offline';
</script>

<script lang="ts">
  // A screen or a block that cannot be shown, told as a library would: what happened, the likely
  // cause, and a way out. Never the raw message of the server, never JSON. The code is printed as
  // a folio (« Erreur 404 »); `incident` is the request id to copy into a report. Used by
  // +error.svelte (a whole screen, h1) and inside a screen (a block, h2).
  import type { Snippet } from 'svelte';
  import { t } from '#lib/i18n/index.ts';
  import Illustration, { type IllustrationName } from '../atoms/Illustration.svelte';

  const ART: Record<ErrorKind, IllustrationName> = {
    'not-found': 'missing',
    forbidden: 'locked',
    'signed-out': 'locked',
    server: 'fallen',
    offline: 'offline'
  };

  let {
    kind,
    title,
    code,
    path = null,
    incident = null,
    headingLevel = 1,
    children,
    actions
  }: {
    kind: ErrorKind;
    title: string;
    /** the folio printed above the title (« Erreur 404 · /serie/7f3a ») */
    code?: string;
    path?: string | null;
    incident?: string | null;
    headingLevel?: 1 | 2;
    children?: Snippet;
    actions?: Snippet;
  } = $props();
</script>

<section class="error-state error-state--{kind}" class:error-state--block={headingLevel === 2}>
  <div class="error-state__text">
    {#if code}
      <p class="error-state__code">{code}{#if path} · <span class="error-state__path">{path}</span>{/if}</p>
    {/if}
    <svelte:element this={`h${headingLevel}`} class="error-state__title">{title}</svelte:element>
    {#if children}<div class="error-state__body">{@render children()}</div>{/if}
    {#if incident}
      <p class="error-state__incident">{t('ui.alert.incident')} <code>{incident}</code></p>
    {/if}
    {#if actions}<div class="error-state__actions">{@render actions()}</div>{/if}
  </div>
  <div class="error-state__art"><Illustration name={ART[kind]} /></div>
</section>

<style>
  .error-state {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(12rem, 20rem);
    gap: var(--space-7);
    align-items: center;
    max-width: 60rem;
    margin: var(--space-6) auto 0;
  }
  .error-state--block {
    margin-top: 0;
    gap: var(--space-5);
    grid-template-columns: minmax(0, 1fr) minmax(8rem, 12rem);
  }
  .error-state__code {
    margin: 0;
    font: italic var(--step-1) / 1.2 var(--font-text);
    color: var(--color-muted);
    font-variant-numeric: oldstyle-nums;
  }
  .error-state__path {
    font-family: var(--font-mono);
    font-style: normal;
    font-size: var(--step-0);
    overflow-wrap: anywhere;
  }
  .error-state__title {
    margin: var(--space-3) 0 0;
    font-size: var(--step-4);
    font-weight: 500;
    letter-spacing: -0.02em;
  }
  .error-state--block .error-state__title {
    font-size: var(--step-3);
  }
  .error-state__body {
    margin-top: var(--space-4);
    max-width: var(--measure);
    font: var(--step-1) / var(--leading-read) var(--font-text);
  }
  .error-state__body :global(p) {
    margin: 0;
  }
  .error-state__body :global(p + p) {
    margin-top: var(--space-3);
  }
  .error-state__incident {
    margin: var(--space-4) 0 0;
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  .error-state__actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
    align-items: center;
    margin-top: var(--space-5);
  }
  .error-state__art {
    color: var(--color-ink);
  }
  @media (max-width: 60rem) {
    .error-state,
    .error-state--block {
      grid-template-columns: minmax(0, 1fr);
      gap: var(--space-5);
    }
    .error-state__art {
      max-width: 14rem;
      order: -1;
    }
  }
</style>
