<script lang="ts">
  // Confirm what cannot be undone. The title names the object (« Supprimer la liste « Été » ? »),
  // the message says what follows, the confirming button repeats the verb. With `phrase`, the
  // person copies the phrase letter for letter and the button stays dead until it matches (the
  // typed confirmation of EF-EXP-031, EF-EXP-037: same labels as TypedConfirm). `onconfirm` may
  // be async: the button shows it is working and the dialog cannot be closed meanwhile; it closes
  // when the promise resolves, and stays open (the screen shows why) when it throws.
  import { t } from '#lib/i18n/index.ts';
  import Button from '../atoms/Button.svelte';
  import Input from '../atoms/Input.svelte';
  import Dialog from './Dialog.svelte';
  import Field from './Field.svelte';

  let {
    open = $bindable(false),
    title,
    message,
    confirmLabel,
    cancelLabel = t('typed_confirm.cancel'),
    tone = 'danger',
    phrase,
    onconfirm,
    oncancel
  }: {
    open?: boolean;
    title: string;
    message?: string;
    confirmLabel: string;
    cancelLabel?: string;
    tone?: 'danger' | 'primary';
    phrase?: string;
    onconfirm: (typed?: string) => void | Promise<void>;
    oncancel?: () => void;
  } = $props();

  let typed = $state('');
  let busy = $state(false);
  const ready = $derived(!phrase || typed === phrase);

  $effect(() => {
    if (!open) typed = '';
  });

  async function confirm(event?: SubmitEvent): Promise<void> {
    event?.preventDefault();
    if (!ready || busy) return;
    busy = true;
    try {
      await onconfirm(phrase ? typed : undefined);
      open = false;
    } catch {
      // the screen says what went wrong; the dialog stays for another try
    } finally {
      busy = false;
    }
  }
</script>

<Dialog bind:open {title} dismissible={!busy} onclose={() => oncancel?.()}>
  {#if message}<p class="confirm__message">{message}</p>{/if}
  {#if phrase}
    <form class="confirm__form" onsubmit={confirm}>
      <p class="confirm__phrase"><code>{phrase}</code></p>
      <Field label={t('typed_confirm.phrase_label')}>
        {#snippet children(control)}
          <Input {...control} bind:value={typed} autocomplete="off" spellcheck="false" />
        {/snippet}
      </Field>
    </form>
  {/if}
  {#snippet footer()}
    <Button variant="secondary" disabled={busy} onclick={() => (open = false)}>{cancelLabel}</Button>
    <Button variant={tone} loading={busy} disabled={!ready} onclick={() => confirm()}>{confirmLabel}</Button>
  {/snippet}
</Dialog>

<style>
  .confirm__message {
    margin: 0;
  }
  .confirm__form {
    display: grid;
    gap: var(--space-3);
  }
  .confirm__phrase {
    margin: 0;
    padding: var(--space-2) var(--space-3);
    background: var(--color-surface-2);
    border-radius: var(--radius-sm);
    overflow-wrap: anywhere;
  }
</style>
