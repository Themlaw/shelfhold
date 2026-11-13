<script lang="ts">
  // One entry of the Health page: its stable code and the way out. The page applies nothing; the
  // command is shown to be run (a reserved one asks a fresh session or the local command line).
  import type { HealthAction } from '#lib/healthPage.ts';
  import { t } from '#lib/i18n/index.ts';

  let {
    label,
    code,
    action
  }: { label: string; code: string | null; action: HealthAction | null } = $props();
</script>

<li class="entry">
  <span class="label">{label}</span>
  {#if code}<code class="code">{code}</code>{/if}
  {#if action}
    <p class="action">
      {#if action.text}{action.text}{/if}
      <span class="command">{t('health_page.run')} <code>{action.command}</code></span>
      {#if action.reserved}<strong class="reserved">{t('health_page.reserved')}</strong>{/if}
    </p>
  {/if}
</li>

<style>
  .entry {
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule);
  }
  .label {
    font-weight: 600;
    margin-inline-end: 0.5rem;
  }
  .code,
  .command code {
    font-family: monospace;
    overflow-wrap: anywhere;
  }
  .action {
    margin: 0.25rem 0 0;
    color: var(--color-muted);
  }
  .command {
    display: block;
  }
  .reserved {
    color: var(--color-err);
  }
</style>
