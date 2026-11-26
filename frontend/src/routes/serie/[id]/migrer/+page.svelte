<script lang="ts">
  // « Migrer vers une autre source » (EF-BIB-014): a series moves to a source that numbers its
  // chapters otherwise. The server proposes the correspondence; this screen only shows it, lets the
  // admin change a line, and sends back the fingerprint (`digest`) of the table he saw, first to
  // validate it, then to apply it. Nothing is applied before the validation, and the server
  // checks both: the screen is no gate. The action is reserved: a stale session asks for the
  // password in place and runs the step again after.
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import {
    editionSheet,
    sourceMigrationApply,
    sourceMigrationPropose,
    sourceMigrationValidate
  } from '#lib/api/index.ts';
  import type { MigrationOut, SheetBindingOut } from '#lib/api/types.gen.ts';

  type Row = MigrationOut['rows'][number];
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import ConfirmDialog from '#lib/ui/molecules/ConfirmDialog.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

  const GONE = ['not_loaded', 'removed_upstream'];
  const REASONS: Record<string, MessageKey> = {
    not_loaded: 'sheet.reason.not_loaded',
    content_failures: 'sheet.reason.content_failures',
    removed_upstream: 'sheet.reason.removed_upstream',
    manual: 'sheet.reason.manual'
  };

  const editionId = $derived(page.params.id ?? '');
  let bindings = $state<SheetBindingOut[]>([]);
  let sourceKey = $state('');
  let remoteId = $state('');
  let replaces = $state('');
  let migration = $state<MigrationOut | null>(null);
  let edits = $state<Record<string, string>>({}); // place -> the number the admin typed
  let problem = $state<ProblemView | null>(null);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;
  let confirmingApply = $state(false);
  let busy = $state(false);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const applied = $derived(migration?.state === 'applied');
  const validated = $derived(migration?.state === 'validated');
  const changed = $derived(
    (migration?.rows ?? []).filter((r) => edits[r.place] !== undefined && edits[r.place] !== r.target)
  );

  onMount(async () => {
    const { data } = await editionSheet({ path: { edition_id: editionId } });
    bindings = data?.bindings ?? [];
    // the usual case: the source that went away is the one to replace
    replaces = bindings.find((b) => !b.active && GONE.includes(b.disabled_reason ?? ''))?.id ?? '';
  });

  /** Run a step; a stale password asks for it in place and the step runs again after. */
  async function guarded(step: () => Promise<{ error?: unknown }>): Promise<boolean> {
    busy = true;
    try {
      const { error } = await step();
      if (error === undefined) {
        problem = null;
        return true;
      }
      if (needsReauth(error)) {
        retry = async () => {
          await guarded(step);
        };
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        problem = problemView(error);
      }
      return false;
    } finally {
      busy = false;
    }
  }

  async function afterReauth(): Promise<void> {
    const again = retry;
    retry = null;
    reauthMethods = null;
    if (again) await again();
  }

  function cancelReauth(): void {
    retry = null;
    reauthMethods = null;
  }

  function take(data: MigrationOut | undefined): void {
    if (!data) return;
    migration = data;
    edits = {};
  }

  async function propose(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    await guarded(async () => {
      const done = await sourceMigrationPropose({
        headers: headers(),
        path: { edition_id: editionId },
        body: {
          source_key: sourceKey.trim(),
          remote_id: remoteId.trim(),
          ...(replaces ? { replaces_binding_id: replaces } : {})
        }
      });
      take(done.data);
      return done;
    });
  }

  async function validate(): Promise<void> {
    if (!migration) return;
    const sent = migration;
    const overrides = changed.map((r) => ({ number: r.number, target: edits[r.place]!.trim() }));
    await guarded(async () => {
      const done = await sourceMigrationValidate({
        headers: headers(),
        path: { edition_id: editionId, migration_id: sent.id },
        body: { digest: sent.digest, overrides }
      });
      take(done.data);
      return done;
    });
  }

  async function apply(): Promise<void> {
    if (!migration) return;
    const sent = migration;
    await guarded(async () => {
      const done = await sourceMigrationApply({
        headers: headers(),
        path: { edition_id: editionId, migration_id: sent.id },
        body: { digest: sent.digest }
      });
      take(done.data);
      return done;
    });
  }

  const why = (row: Row, offset: number): string =>
    row.basis === 'offset'
      ? t('migrate.basis.offset', { offset })
      : t(`migrate.basis.${row.basis}` as MessageKey);
  const source = (b: SheetBindingOut): string =>
    t('migrate.replaces_item', {
      source: b.source_key,
      reason: b.disabled_reason ? t(REASONS[b.disabled_reason] ?? 'sheet.reason.manual') : '—'
    });
</script>

<svelte:head>
  <title>{t('migrate.title')}</title>
</svelte:head>

<main class="page">
  <PageHead
    title={t('migrate.title')}
    lede={t('migrate.intro')}
    crumbs={[
      { label: t('settings.title'), href: '/reglages' },
      { label: t('migrate.back'), href: `/serie/${editionId}` },
      { label: t('migrate.title') }
    ]}
  />

  <div class="stack">
    {#if problem}<ProblemAlert {problem} level={2} />{/if}
    {#if reauthMethods}
      <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
    {/if}
  </div>

  {#if !migration}
    <form class="stack page--form propose" onsubmit={propose}>
      <Field label={t('migrate.source_key')} required>
        {#snippet children(control)}
          <Input {...control} bind:value={sourceKey} maxlength={500} autocomplete="off" />
        {/snippet}
      </Field>
      <Field label={t('migrate.remote_id')} required>
        {#snippet children(control)}
          <Input {...control} bind:value={remoteId} maxlength={1000} autocomplete="off" />
        {/snippet}
      </Field>
      <Field label={t('migrate.replaces')}>
        {#snippet children(control)}
          <Select
            {...control}
            bind:value={replaces}
            options={[
              { value: '', label: t('migrate.replaces_none') },
              ...bindings.map((binding) => ({ value: binding.id, label: source(binding) }))
            ]}
          />
        {/snippet}
      </Field>
      <div class="btn-row">
        <Button type="submit" variant="primary" disabled={busy}>{t('migrate.propose')}</Button>
      </div>
    </form>
  {:else}
    <div class="stack">
      {#if !applied}
        <p>{t('migrate.summary', { held: migration.held_count, fetch: migration.to_fetch })}</p>
        {#if migration.unmatched_units.length > 0}
          <p class="hint">{t('migrate.unmatched', { count: migration.unmatched_units.length })}</p>
        {/if}
      {/if}
      <table class="mapping">
        <caption class="section-head">{t('migrate.table')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('migrate.col.source')}</th>
            <th scope="col">{t('migrate.col.library')}</th>
            <th scope="col">{t('migrate.col.basis')}</th>
          </tr>
        </thead>
        <tbody>
          {#each migration.rows as row (row.place)}
            <tr>
              <td data-label={t('migrate.col.source')}>{row.number || t('migrate.unnumbered')}</td>
              <td data-label={t('migrate.col.library')}>
                {#if row.number && migration.state === 'proposed'}
                  <Input
                    aria-label={t('migrate.row_target', { number: row.number })}
                    value={edits[row.place] ?? row.target}
                    oninput={(e) => (edits[row.place] = e.currentTarget.value)}
                    inputmode="decimal"
                    maxlength={64}
                    width="short"
                  />
                {:else}
                  {row.target || t('migrate.unnumbered')}
                {/if}
              </td>
              <td data-label={t('migrate.col.basis')}>{why(row, migration.offset)}</td>
            </tr>
          {/each}
        </tbody>
      </table>

      {#if applied}
        <Alert tone="success" live="polite" title={t('migrate.done', { count: migration.to_fetch })} />
      {:else if validated}
        <p>{t('migrate.validated')}</p>
        <div class="btn-row">
          <Button variant="primary" onclick={() => (confirmingApply = true)} disabled={busy} loading={busy}>{t('migrate.apply')}</Button>
        </div>
      {:else}
        <div class="btn-row">
          <Button variant="primary" onclick={validate} disabled={busy} loading={busy}>{t('migrate.validate')}</Button>
        </div>
      {/if}
    </div>
  {/if}
</main>

<ConfirmDialog
  bind:open={confirmingApply}
  title={t('migrate.apply_title')}
  message={t('migrate.apply_message', { count: migration?.to_fetch ?? 0 })}
  confirmLabel={t('migrate.apply_confirm')}
  tone="primary"
  onconfirm={apply}
/>

<style>
  .propose {
    margin-top: var(--space-4);
  }
  .mapping {
    width: 100%;
    border-collapse: collapse;
  }
  .mapping caption {
    text-align: left;
    font: 700 var(--step-1) / 1.2 var(--font-text);
  }
  .mapping th {
    padding: var(--space-2) var(--space-3);
    text-align: left;
    font: 600 var(--step--1) / var(--leading-ui) var(--font-ui);
    color: var(--color-muted);
    border-bottom: var(--rule-hair) solid var(--color-ink);
  }
  .mapping td {
    padding: var(--space-2) var(--space-3);
    min-height: var(--target);
    vertical-align: middle;
    border-bottom: var(--rule-hair) solid var(--color-rule);
    overflow-wrap: anywhere;
  }
  @media (max-width: 40rem) {
    .mapping thead {
      position: absolute;
      inline-size: 1px;
      block-size: 1px;
      overflow: hidden;
      clip-path: inset(50%);
    }
    .mapping tr,
    .mapping td {
      display: block;
    }
    .mapping tr {
      padding: var(--space-2) 0;
      border-bottom: var(--rule-hair) solid var(--color-rule-strong);
    }
    .mapping td {
      display: grid;
      grid-template-columns: minmax(7rem, 40%) minmax(0, 1fr);
      align-items: center;
      gap: var(--space-3);
      border-bottom: 0;
    }
    .mapping td::before {
      content: attr(data-label);
      color: var(--color-muted);
      font: 600 var(--step--1) / var(--leading-ui) var(--font-ui);
    }
  }
</style>
