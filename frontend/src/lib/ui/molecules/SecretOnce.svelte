<script lang="ts" module>
  export type SecretField = {
    label: string;
    value: string;
    /** the secret itself: set in a larger mono, selected at one click, with a copy button */
    secret?: boolean;
    testid?: string;
  };
</script>
<script lang="ts">
  // A secret that is shown once (a device token, a webhook key): framed like a receipt, with the
  // warning above it, each value in mono, a button to copy it and the one gesture that closes the
  // frame (« J'ai noté le secret »). The page keeps the value in memory only and calls `onack`
  // to drop it; nothing here stores or sends it.
  import { t } from '#lib/i18n/index.ts';
  import Button from '../atoms/Button.svelte';
  import Icon from '../atoms/Icon.svelte';
  import { notify } from '../toast.svelte.ts';

  let {
    title,
    note,
    fields,
    footnote,
    ackLabel,
    onack
  }: {
    title: string;
    note: string;
    fields: readonly SecretField[];
    footnote?: string;
    ackLabel: string;
    onack: () => void;
  } = $props();

  const titleId = $props.id();

  async function copy(field: SecretField): Promise<void> {
    try {
      await navigator.clipboard.writeText(field.value);
      notify({ title: t('ui.secret.copied', { what: field.label }) });
    } catch {
      notify({ title: t('ui.secret.copy_failed'), tone: 'warning' });
    }
  }
</script>

<section class="once" aria-labelledby="once-{titleId}">
  <div class="once__head">
    <Icon name="key" />
    <h2 id="once-{titleId}">{title}</h2>
  </div>
  <p class="once__note">{note}</p>
  <dl class="once__fields">
    {#each fields as field (field.label)}
      <div class="once__field">
        <dt>{field.label}</dt>
        <dd>
          <code class="once__value" class:once__value--secret={field.secret} data-testid={field.testid}>{field.value}</code>
          <Button size="sm" variant="ghost" icon="copy" aria-label={t('ui.secret.copy', { what: field.label })} onclick={() => copy(field)}>
            {t('ui.secret.copy_short')}
          </Button>
        </dd>
      </div>
    {/each}
  </dl>
  {#if footnote}<p class="once__foot">{footnote}</p>{/if}
  <div class="btn-row">
    <Button variant="primary" icon="check" onclick={onack}>{ackLabel}</Button>
  </div>
</section>

<style>
  .once {
    display: grid;
    gap: var(--space-3);
    padding: var(--space-5);
    border: var(--rule-mid) solid var(--color-warn);
    border-radius: var(--radius-sm);
    background: var(--color-surface);
    box-shadow: var(--shadow-2);
  }
  .once__head {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--color-warn);
  }
  h2 {
    margin: 0;
    font-size: var(--step-2);
    color: var(--color-ink);
  }
  .once__note,
  .once__foot {
    margin: 0;
    max-width: var(--measure);
  }
  .once__foot {
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  .once__fields {
    display: grid;
    gap: var(--space-3);
    margin: 0;
  }
  dt {
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  dd {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
    margin: 0;
  }
  .once__value {
    flex: 1 1 12rem;
    min-width: 0;
    padding: var(--space-2) var(--space-3);
    border: var(--rule-hair) dashed var(--color-rule-strong);
    border-radius: var(--radius-xs);
    background: var(--color-bg);
    overflow-wrap: anywhere;
    user-select: all;
  }
  .once__value--secret {
    font-size: var(--step-1);
    letter-spacing: 0.05em;
  }
</style>
