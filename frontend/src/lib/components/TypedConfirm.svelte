<script lang="ts">
  // Typed confirmation (EF-EXP-031, EF-EXP-037): the server gives the phrase; the person copies
  // it letter for letter, and the button stays dead until what was typed is that phrase.
  import { t } from '#lib/i18n/index.ts';

  let {
    phrase,
    onconfirm,
    oncancel,
    confirmLabel = t('typed_confirm.confirm')
  }: {
    phrase: string;
    onconfirm: (typed: string) => void;
    oncancel?: () => void;
    confirmLabel?: string;
  } = $props();

  let typed = $state('');

  const exact = $derived(typed === phrase);

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    if (exact) onconfirm(typed);
  }
</script>

<p>{phrase}</p>
<form onsubmit={submit}>
  <label for="typed-phrase">{t('typed_confirm.phrase_label')}</label>
  <input
    id="typed-phrase"
    type="text"
    autocomplete="off"
    bind:value={typed}
  />
  <button type="submit" disabled={!exact}>{confirmLabel}</button>
  {#if oncancel}
    <button type="button" onclick={oncancel}>{t('typed_confirm.cancel')}</button>
  {/if}
</form>

<style>
  form {
    display: grid;
    gap: 0.5rem;
  }
</style>