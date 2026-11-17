<script lang="ts">
  // The state of the progress sync, member by member (EF-EXP-026): for each Kavita binding open to
  // a member, whether a token is linked and when the last pull and the last push were. The server
  // decides who is shown: a member reads their own line, an admin reads every member's; a child
  // and anyone it refuses see nothing. The screen draws what it is given: no key, no fingerprint,
  // no account name is ever part of the answer, and a missing date is said as "never".
  import { onMount } from 'svelte';
  import { readSyncStatus } from '#lib/api/index.ts';
  import type { SyncStatusOut } from '#lib/api/types.gen.ts';
  import { formatDateTime, t } from '#lib/i18n/index.ts';

  let rows = $state<SyncStatusOut[] | null>(null); // null: nothing to draw (not loaded, or refused)

  const groups = $derived.by(() => {
    const out: { userId: string; username: string; rows: SyncStatusOut[] }[] = [];
    for (const row of rows ?? []) {
      const last = out[out.length - 1];
      if (last && last.userId === row.user_id) last.rows.push(row);
      else out.push({ userId: row.user_id, username: row.username, rows: [row] });
    }
    return out; // the server sends a member's rows together
  });

  const when = (value: string | null | undefined): string =>
    value ? formatDateTime(value) : t('sync.never');

  onMount(async () => {
    const { data, error } = await readSyncStatus();
    if (error === undefined && data) rows = data.items;
  });
</script>

{#if rows}
  <section aria-labelledby="sync-status-title">
    <h2 id="sync-status-title">{t('sync.title')}</h2>
    <p>{t('sync.intro')}</p>
    {#if rows.length === 0}
      <p>{t('sync.empty')}</p>
    {/if}
    {#each groups as group (group.userId)}
      <div class="member">
        <h3>{group.username}</h3>
        <ul>
          {#each group.rows as row (row.shelf_destination_id)}
            <li>
              <h4>{t('sync.binding', { shelf: row.shelf_name, server: row.target_name })}</h4>
              <p class="state" data-state={row.state}>{t(`sync.state.${row.state}`)}</p>
              <p>{t('sync.last_pull', { when: when(row.last_pull_at) })}</p>
              <p>{t('sync.last_push', { when: when(row.last_push_at) })}</p>
            </li>
          {/each}
        </ul>
      </div>
    {/each}
  </section>
{/if}

<style>
  section { margin: 1.5rem 0; padding: 1rem; border: 1px solid var(--color-rule); }
  ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.75rem; }
  li { padding: 0.5rem 0; border-top: 1px solid var(--color-rule); }
  h3, h4, p { margin: 0.25rem 0; }
  .state { font-weight: 600; }
</style>
