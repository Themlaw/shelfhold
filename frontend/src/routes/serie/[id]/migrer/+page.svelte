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

<main>
  <p>
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
    <a href={`/serie/${editionId}`}>{t('migrate.back')}</a>
  </p>
  <h1>{t('migrate.title')}</h1>
  <p>{t('migrate.intro')}</p>

  {#if problem}<ProblemCard {problem} level={2} />{/if}
  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}

  {#if !migration}
    <form onsubmit={propose}>
      <label>
        {t('migrate.source_key')}
        <input bind:value={sourceKey} required maxlength="500" autocomplete="off" />
      </label>
      <label>
        {t('migrate.remote_id')}
        <input bind:value={remoteId} required maxlength="1000" autocomplete="off" />
      </label>
      <label>
        {t('migrate.replaces')}
        <select bind:value={replaces}>
          <option value="">{t('migrate.replaces_none')}</option>
          {#each bindings as binding (binding.id)}
            <option value={binding.id}>{source(binding)}</option>
          {/each}
        </select>
      </label>
      <button type="submit" disabled={busy}>{t('migrate.propose')}</button>
    </form>
  {:else}
    {#if !applied}
      <p>{t('migrate.summary', { held: migration.held_count, fetch: migration.to_fetch })}</p>
      {#if migration.unmatched_units.length > 0}
        <p>{t('migrate.unmatched', { count: migration.unmatched_units.length })}</p>
      {/if}
    {/if}
    <table>
      <caption>{t('migrate.table')}</caption>
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
            <td>{row.number || t('migrate.unnumbered')}</td>
            <td>
              {#if row.number && migration.state === 'proposed'}
                <input
                  aria-label={t('migrate.row_target', { number: row.number })}
                  value={edits[row.place] ?? row.target}
                  oninput={(e) => (edits[row.place] = e.currentTarget.value)}
                  inputmode="decimal"
                  maxlength="64"
                />
              {:else}
                {row.target || t('migrate.unnumbered')}
              {/if}
            </td>
            <td>{why(row, migration.offset)}</td>
          </tr>
        {/each}
      </tbody>
    </table>

    {#if applied}
      <p role="status">{t('migrate.done', { count: migration.to_fetch })}</p>
    {:else if validated}
      <p>{t('migrate.validated')}</p>
      <button type="button" onclick={apply} disabled={busy}>{t('migrate.apply')}</button>
    {:else}
      <button type="button" onclick={validate} disabled={busy}>{t('migrate.validate')}</button>
    {/if}
  {/if}
</main>
