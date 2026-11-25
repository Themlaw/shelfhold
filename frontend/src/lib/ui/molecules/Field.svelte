<script lang="ts" module>
  /** What Field hands to its control: spread it on the Input, Select or Textarea. */
  export type FieldControl = {
    id: string;
    'aria-describedby': string | undefined;
    'aria-invalid': true | undefined;
    required: boolean | undefined;
  };
</script>

<script lang="ts">
  // A field: the label above (always visible), the help under the label, the control, the error
  // under the control with its icon. The control receives its id and its descriptions:
  //   <Field label="Nom" error={err}>{#snippet children(c)}<Input {...c} bind:value />{/snippet}</Field>
  import type { Snippet } from 'svelte';
  import { t } from '#lib/i18n/index.ts';
  import Icon from '../atoms/Icon.svelte';

  let {
    label,
    hint,
    error,
    optional = false,
    required,
    count,
    id: givenId,
    children
  }: {
    label: string;
    hint?: string;
    error?: string | null;
    optional?: boolean;
    required?: boolean;
    /** « 12 / 64 »: the characters typed and allowed, shown at the end of the label row */
    count?: string;
    id?: string;
    children: Snippet<[FieldControl]>;
  } = $props();

  const auto = $props.id();
  const id = $derived(givenId ?? `field-${auto}`);
  const described = $derived(
    [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean).join(' ') || undefined
  );
</script>

<div class="field">
  <div class="field__label-row">
    <label class="field__label" for={id}>
      {label}
      {#if optional}<span class="field__optional">{t('ui.field.optional')}</span>{/if}
    </label>
    {#if count}<span class="field__count">{count}</span>{/if}
  </div>
  {#if hint}<p class="field__hint" id="{id}-hint">{hint}</p>{/if}
  {@render children({
    id,
    'aria-describedby': described,
    'aria-invalid': error ? true : undefined,
    required
  })}
  {#if error}
    <p class="field__error" id="{id}-error"><Icon name="alert" size="sm" /><span>{error}</span></p>
  {/if}
</div>

<style>
  .field {
    display: grid;
    gap: var(--space-2);
  }
  :global(.field + .field) {
    margin-top: var(--space-5);
  }
  .field__label-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: var(--space-3);
  }
  .field__label {
    font-weight: 700;
  }
  .field__optional {
    font-weight: 400;
    color: var(--color-subtle);
    font-style: italic;
    margin-left: var(--space-1);
  }
  .field__hint {
    margin: calc(-1 * var(--space-1)) 0 0;
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  .field__error {
    display: flex;
    gap: var(--space-2);
    align-items: flex-start;
    margin: 0;
    color: var(--color-err);
    font-size: var(--step--1);
    font-weight: 500;
  }
  .field__count {
    color: var(--color-subtle);
    font-size: var(--step--1);
    font-variant-numeric: tabular-nums;
  }
</style>
