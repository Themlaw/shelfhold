<script lang="ts">
  // Settings in three levels (EF-UX-009). The server decides what each row is: its level, whether
  // it is locked (set by the environment or fixed by this release) and whether it is secret. The
  // screen draws that and decides nothing, so a new setting needs no change here.
  import { onMount } from 'svelte';
  import { readConfig, patchConfig } from '#lib/api/index.ts';
  import type { ConfigRow } from '#lib/api/types.gen.ts';
  import { hasMessage, t } from '#lib/i18n/index.ts';
  import { isForbidden, needsReauth, problemOf } from '#lib/triage.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';

  const LEVELS = ['essential', 'advanced', 'expert'] as const;

  let items = $state<ConfigRow[]>([]);
  let adminOnly = $state(false);
  let failed = $state(false);
  let message = $state('');
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  const originOf = (row: ConfigRow): string => row.overridden_by ?? row.source;
  const originText = (row: ConfigRow): string => {
    const key = `settings.origin.${originOf(row)}`;
    return t('settings.origin', { origin: hasMessage(key) ? t(key) : originOf(row) });
  };
  const helpOf = (row: ConfigRow): string | null => {
    const key = `settings.help.${row.key}`;
    return hasMessage(key) ? t(key) : null;
  };
  const shown = (value: unknown): string =>
    typeof value === 'string' ? value : JSON.stringify(value ?? '');

  async function load(): Promise<void> {
    try {
      const { data } = await readConfig({ throwOnError: true });
      items = data.items;
    } catch (error) {
      failed = true;
      if (isForbidden(error)) adminOnly = true;
      message = isForbidden(error) ? t('settings.admin_only') : t('settings.error');
    }
  }

  onMount(load);

  async function save(key: string, value: string): Promise<void> {
    failed = false;
    message = '';
    try {
      const { data } = await patchConfig({
        headers: headers(),
        body: { values: { [key]: value } },
        throwOnError: true
      });
      items = data.items;
      message = t('settings.saved');
    } catch (error) {
      if (needsReauth(error)) {
        retry = () => save(key, value);
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
        return;
      }
      failed = true;
      message =
        problemOf(error).code === 'config.key_readonly'
          ? t('settings.locked_refused')
          : t('settings.refused');
    }
  }

  function afterReauth(): void {
    const again = retry;
    retry = null;
    reauthMethods = null;
    if (again) void again();
  }

  function cancelReauth(): void {
    retry = null;
    reauthMethods = null;
  }

  const groups = $derived.by(() => {
    const grouped: Record<ConfigRow['level'], ConfigRow[]> = {
      essential: [],
      advanced: [],
      expert: []
    };
    for (const row of items) grouped[row.level].push(row);
    return grouped;
  });
</script>

<svelte:head>
  <title>{t('settings.title')}</title>
</svelte:head>

<main>
  <h1>{t('settings.title')}</h1>

  {#if !adminOnly}
    {#each LEVELS as level (level)}
      <section data-level={level} aria-labelledby={`level-${level}`}>
        <h2 id={`level-${level}`}>{t(`settings.level.${level}`)}</h2>
        {#each groups[level] as row (row.key)}
          <div class="field" data-testid="setting-field" data-key={row.key}>
            {#if row.secret}
              <span class="label">{row.key}</span>
              <span class="origin">{t('settings.secret')}</span>
            {:else}
              <label for={row.key}>{row.key}</label>
              <input
                id={row.key}
                type="text"
                value={shown(row.value)}
                disabled={row.locked}
                onchange={(e) => save(row.key, (e.target as HTMLInputElement).value)}
              />
              {#if row.locked}
                <span class="origin" data-origin={originOf(row)}>{originText(row)}</span>
              {/if}
            {/if}
            {#if helpOf(row)}
              <span class="help">{helpOf(row)}</span>
            {/if}
            {#if row.warning}
              <span class="warn">{row.warning}</span>
            {/if}
          </div>
        {/each}
      </section>
    {/each}
  {/if}

  <nav aria-label={t('settings.mine')}>
    <h2>{t('settings.mine')}</h2>
    <ul>
      {#if !adminOnly}
        <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
        <li><a href="/reglages/profils">{t('settings.link.profiles')}</a></li>
        <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
        <li><a href="/activite/file">{t('settings.link.queue')}</a></li>
      {/if}
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
      <li><a href="/reglages/notifications">{t('settings.link.notifications')}</a></li>
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
      <li><a href="/reglages/apparence">{t('settings.link.appearance')}</a></li>
    </ul>
  </nav>

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}

  {#if message}
    <p role={failed ? 'alert' : 'status'} class:err={failed}>{message}</p>
  {/if}
</main>

<style>
  main {
    max-width: 48rem;
    margin: 0 auto;
    padding: 2rem 1rem;
  }
  section {
    margin: 1.5rem 0;
  }
  .field {
    display: grid;
    gap: 0.25rem;
    margin: 0.75rem 0;
  }
  input:disabled {
    opacity: 0.6;
  }
  .origin,
  .help {
    color: var(--color-muted);
    font-size: 0.875rem;
  }
  .err {
    color: var(--color-err);
  }
</style>
