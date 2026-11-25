<script lang="ts">
  // Typed confirmation (EF-EXP-031, EF-EXP-037): the server gives the phrase; the person copies
  // it letter for letter, and the button stays dead until what was typed is that phrase.
  import { t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';

  let {
    phrase,
    onconfirm,
    oncancel,
    busy = false,
    confirmLabel = t('typed_confirm.confirm')
  }: {
    phrase: string;
    onconfirm: (typed: string) => void;
    oncancel?: () => void;
    busy?: boolean;
    confirmLabel?: string;
  } = $props();

  // Several of these can be on one page: the label must point at its own field.
  const fieldId = $props.id();

  let typed = $state('');

  const exact = $derived(typed === phrase);

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    if (exact && !busy) onconfirm(typed);
  }
</script>

<p class="phrase">{phrase}</p>
<form onsubmit={submit}>
  <Field label={t('typed_confirm.phrase_label')} id={fieldId}>
    {#snippet children(control)}
      <Input {...control} mono spellcheck={false} type="text" autocomplete="off" bind:value={typed} />
    {/snippet}
  </Field>
  <div class="btn-row">
    <Button type="submit" variant="danger" disabled={!exact || busy} loading={busy && exact}>{confirmLabel}</Button>
    {#if oncancel}
      <Button variant="ghost" onclick={oncancel}>{t('typed_confirm.cancel')}</Button>
    {/if}
  </div>
</form>

<style>
  .phrase {
    margin: 0 0 var(--space-3);
    padding: var(--space-2) var(--space-3);
    background: var(--color-surface-2);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--step--1);
    overflow-wrap: anywhere;
    user-select: all;
  }
  form {
    display: grid;
    gap: var(--space-3);
    justify-items: start;
    inline-size: min(100%, 30rem);
  }
</style>
