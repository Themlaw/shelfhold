<script lang="ts">
  // A page of settings: rows of « label and help on the left, control on the right »
  // (SettingRow), a thin rule between them, one width (--form-max) even on a wide screen. A
  // form when `onsubmit` is given: the save button ends it (sticky at the bottom of a phone,
  // ENF-A11Y-004), it keeps its label while saving and is never disabled ahead of time.
  // `danger` is the danger zone, apart at the end of the page.
  import type { Snippet } from 'svelte';
  import Button from '../atoms/Button.svelte';

  let {
    label,
    saveLabel,
    saving = false,
    onsubmit,
    children,
    foot,
    danger,
    dangerTitle
  }: {
    /** the accessible name of the form */
    label: string;
    saveLabel?: string;
    saving?: boolean;
    onsubmit?: () => void | Promise<void>;
    children: Snippet;
    /** something next to the button (« saved at 21:04 ») */
    foot?: Snippet;
    danger?: Snippet;
    dangerTitle?: string;
  } = $props();

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    if (!saving) void onsubmit?.();
  }
</script>

<div class="settings">
  {#if onsubmit}
    <form aria-label={label} onsubmit={submit} novalidate>
      <div class="settings__rows">{@render children()}</div>
      <div class="settings__foot">
        {#if foot}<div class="settings__note">{@render foot()}</div>{/if}
        <Button type="submit" variant="primary" loading={saving} class="main-action">{saveLabel}</Button>
      </div>
    </form>
  {:else}
    <div class="settings__rows" role="group" aria-label={label}>{@render children()}</div>
  {/if}
  {#if danger}
    <section class="danger-zone" aria-labelledby="danger-zone-title">
      <h2 id="danger-zone-title">{dangerTitle}</h2>
      {@render danger()}
    </section>
  {/if}
</div>

<style>
  .settings {
    max-width: var(--form-max);
  }
  .settings__rows > :global(.setting:first-child) {
    border-top-color: var(--color-ink);
  }
  .settings__foot {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    align-items: center;
    gap: var(--space-3);
    padding-top: var(--space-5);
    border-top: var(--rule-hair) solid var(--color-ink);
  }
  .settings__note {
    margin-right: auto;
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  .danger-zone {
    margin-top: var(--space-7);
    padding: var(--space-5);
    border: var(--rule-hair) solid var(--color-err);
    border-radius: var(--radius-md);
  }
  .danger-zone h2 {
    margin: 0;
    font-size: var(--step-2);
    color: var(--color-err);
  }
  .danger-zone :global(.danger-zone__row) {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
    margin-top: var(--space-4);
    padding-top: var(--space-4);
    border-top: var(--rule-hair) solid var(--color-rule);
  }
</style>
