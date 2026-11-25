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
  import ProblemCard from '#lib/components/ProblemCard.svelte';
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
  import DataTable from '#lib/ui/organisms/DataTable.svelte';
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
  let busy = $state(false);
  let confirmingApply = $state(false);

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

  const COLUMNS = [
    { key: 'source', label: t('migrate.col.source'), primary: true },
    { key: 'library', label: t('migrate.col.library') },
    { key: 'basis', label: t('migrate.col.basis') }
  ] as const;
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

<main class="page page--read">
  <PageHead
    title={t('migrate.title')}
    lede={t('migrate.intro')}
    crumbs={[{ label: t('migrate.back'), href: `/serie/${editionId}` }, { label: t('migrate.title') }]}
  />

  <div class="stack-lg">
    {#if problem}<ProblemCard {problem} level={2} />{/if}
    {#if reauthMethods}
      <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
    {/if}

    {#if !migration}
      <form class="panel form" aria-label={t('migrate.propose')} onsubmit={propose}>
        <Field label={t('migrate.source_key')} required>
          {#snippet children(control)}
            <Input {...control} mono bind:value={sourceKey} maxlength={500} autocomplete="off" />
          {/snippet}
        </Field>
        <Field label={t('migrate.remote_id')} required>
          {#snippet children(control)}
            <Input {...control} mono bind:value={remoteId} maxlength={1000} autocomplete="off" />
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
        <div><Button type="submit" variant="primary" loading={busy}>{t('migrate.propose')}</Button></div>
      </form>
    {:else}
      {#if !applied}
        <div class="summary">
          <p class="figures num">{t('migrate.summary', { held: migration.held_count, fetch: migration.to_fetch })}</p>
          {#if migration.unmatched_units.length > 0}
            <Alert tone="warning" title={t('migrate.unmatched', { count: migration.unmatched_units.length })} />
          {/if}
        </div>
      {/if}
      <DataTable
        caption={t('migrate.table')}
        columns={COLUMNS}
        rows={migration.rows.map((row) => ({ ...row, id: row.place }))}
        key={(row) => String(row.id)}
      >
        {#snippet cell(column, row)}
          {#if column.key === 'source'}
            {String(row.number) || t('migrate.unnumbered')}
          {:else if column.key === 'library'}
            {#if row.number && migration?.state === 'proposed'}
              <Input
                aria-label={t('migrate.row_target', { number: String(row.number) })}
                value={edits[String(row.place)] ?? String(row.target)}
                oninput={(e: Event) => (edits[String(row.place)] = (e.currentTarget as HTMLInputElement).value)}
                inputmode="decimal"
                maxlength={64}
                width="short"
              />
            {:else}
              {String(row.target) || t('migrate.unnumbered')}
            {/if}
          {:else}
            {why(row as unknown as Row, migration?.offset ?? 0)}
          {/if}
        {/snippet}
      </DataTable>

      {#if applied}
        <Alert tone="success" live="polite" title={t('migrate.done', { count: migration.to_fetch })} />
      {:else if validated}
        <Alert tone="info" title={t('migrate.validated')} />
        <div><Button variant="primary" onclick={() => (confirmingApply = true)} disabled={busy}>{t('migrate.apply')}</Button></div>
      {:else}
        <div><Button variant="primary" onclick={validate} loading={busy}>{t('migrate.validate')}</Button></div>
      {/if}
    {/if}
  </div>
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
  .form {
    display: grid;
    gap: var(--space-4);
    max-width: var(--form-max);
  }
  .figures {
    margin: 0 0 var(--space-3);
    font: 500 var(--step-2) / var(--leading-tight) var(--font-text);
  }
</style>
