<script lang="ts" module>
  export type AlertTone = 'info' | 'success' | 'warning' | 'danger';
</script>

<script lang="ts">
  // A message that belongs to the page: what happened, why, and the way out. A thick left rule,
  // an icon and a title; the colour is never alone. `live` announces it when it appears (a
  // refusal after an action: 'assertive'; a confirmation: 'polite'); a message shown with the
  // page needs none. `incident` prints the request id, to copy into a report.
  import type { Snippet } from 'svelte';
  import { t } from '#lib/i18n/index.ts';
  import Icon from '../atoms/Icon.svelte';
  import type { IconName } from '../atoms/icons.ts';

  const ICON: Record<AlertTone, IconName> = { info: 'info', success: 'check', warning: 'alert', danger: 'alert' };

  let {
    tone = 'info',
    title,
    live = false,
    incident = null,
    headingLevel,
    children,
    actions
  }: {
    tone?: AlertTone;
    title: string;
    live?: false | 'polite' | 'assertive';
    incident?: string | null;
    /** a heading level for the title (2 to 4); none: a strong paragraph */
    headingLevel?: 2 | 3 | 4;
    children?: Snippet;
    actions?: Snippet;
  } = $props();

  const role = $derived(live === 'assertive' ? 'alert' : live === 'polite' ? 'status' : undefined);
</script>

<div class="alert alert--{tone}" {role}>
  <Icon name={ICON[tone]} />
  {#if headingLevel}
    <svelte:element this={`h${headingLevel}`} class="alert__title">{title}</svelte:element>
  {:else}
    <p class="alert__title">{title}</p>
  {/if}
  {#if children}<div class="alert__body">{@render children()}</div>{/if}
  {#if incident}
    <p class="alert__incident">{t('ui.alert.incident')} <code>{incident}</code></p>
  {/if}
  {#if actions}<div class="alert__actions">{@render actions()}</div>{/if}
</div>

<style>
  .alert {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--space-1) var(--space-3);
    align-items: start;
    padding: var(--space-3) var(--space-4);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-left-width: var(--rule-thick);
    background: var(--color-surface);
    border-radius: var(--radius-xs);
  }
  .alert > :global(.icon) {
    grid-row: span 2;
    margin-top: 0.1rem;
  }
  .alert__title {
    margin: 0;
    font: 700 var(--step-0) / var(--leading-ui) var(--font-ui);
  }
  .alert__body,
  .alert__incident,
  .alert__actions {
    grid-column: 2;
  }
  .alert__body {
    color: var(--color-muted);
  }
  .alert__body :global(p) {
    margin: 0;
  }
  .alert__body :global(p + p) {
    margin-top: var(--space-2);
  }
  .alert__incident {
    margin: var(--space-1) 0 0;
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  .alert__actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }
  .alert--info {
    border-color: var(--color-accent);
  }
  .alert--info > :global(.icon) {
    color: var(--color-accent-text);
  }
  .alert--success {
    border-color: var(--color-ok);
    background: var(--color-ok-soft);
  }
  .alert--success > :global(.icon) {
    color: var(--color-ok);
  }
  .alert--warning {
    border-color: var(--color-warn);
    background: var(--color-warn-soft);
  }
  .alert--warning > :global(.icon) {
    color: var(--color-warn);
  }
  .alert--danger {
    border-color: var(--color-err);
    background: var(--color-err-soft);
  }
  .alert--danger > :global(.icon),
  .alert--danger .alert__title {
    color: var(--color-err);
  }
  .alert--warning .alert__body,
  .alert--danger .alert__body,
  .alert--success .alert__body,
  .alert--warning .alert__incident,
  .alert--danger .alert__incident,
  .alert--success .alert__incident {
    color: var(--color-ink);
  }
  :global(.alert + .alert) {
    margin-top: var(--space-3);
  }
</style>
