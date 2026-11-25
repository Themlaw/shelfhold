<script lang="ts">
  // One entry of the Health page: its stable code and the way out. The page applies nothing; the
  // command is shown to be run (a reserved one asks a fresh session or the local command line).
  import type { HealthAction } from '#lib/healthPage.ts';
  import { t } from '#lib/i18n/index.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';

  let {
    label,
    code,
    action
  }: { label: string; code: string | null; action: HealthAction | null } = $props();
</script>

<li class="entry">
  <div class="entry__head">
    {#if label}<span class="entry__label">{label}</span>{/if}
    {#if code}<code class="entry__code">{code}</code>{/if}
  </div>
  {#if action}
    <p class="entry__action">
      {#if action.text}{action.text}{/if}
      <span class="entry__command">{t('health_page.run')} <code>{action.command}</code></span>
      {#if action.reserved}<Badge tone="warning" icon="lock">{t('health_page.reserved')}</Badge>{/if}
    </p>
  {/if}
</li>

<style>
  .entry {
    padding: var(--space-3) 0;
    border-bottom: var(--rule-hair) solid var(--color-rule);
  }
  .entry__head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-1) var(--space-3);
  }
  .entry__label {
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  .entry__code,
  .entry__command code {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--step--1);
    overflow-wrap: anywhere;
  }
  .entry__code {
    color: var(--color-muted);
  }
  .entry__action {
    margin: var(--space-1) 0 0;
    color: var(--color-muted);
  }
  .entry__command {
    display: block;
  }
  .entry__command code {
    padding: var(--space-1) var(--space-2);
    background: var(--color-surface-2);
    border-radius: var(--radius-sm);
    color: var(--color-ink);
  }
</style>
