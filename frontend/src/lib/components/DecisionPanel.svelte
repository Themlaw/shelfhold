<script lang="ts">
  // The other decisions on a request that waits: refuse (with the reason that the requester will
  // read), mark it not found, or put it aside. Shared by the queue and by the review of a list.
  import { t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';

  let {
    reason = $bindable(''),
    onreject,
    onnotfound,
    onclose
  }: {
    reason?: string;
    onreject: () => void;
    onnotfound: () => void;
    onclose: () => void;
  } = $props();

  let attempted = $state(false);
  const missingReason = $derived(attempted && reason.trim() === '');

  function refuse(): void {
    attempted = true;
    if (reason.trim() === '') return;
    onreject();
  }
</script>

<section class="decision" aria-label={t('approve.refuse_group')}>
  <Field label={t('approve.reason')} error={missingReason ? t('approve.reason_needed') : null}>
    {#snippet children(control)}<Input {...control} bind:value={reason} />{/snippet}
  </Field>
  <div class="btn-row">
    <Button variant="danger" onclick={refuse}>{t('approve.reject')}</Button>
    <Button onclick={onnotfound}>{t('approve.not_found')}</Button>
    <Button variant="ghost" onclick={onclose}>{t('approve.close')}</Button>
  </div>
</section>

<style>
  .decision {
    display: grid;
    gap: var(--space-3);
    margin-top: var(--space-4);
    padding-top: var(--space-4);
    border-top: var(--rule-hair) solid var(--color-rule);
  }
</style>
