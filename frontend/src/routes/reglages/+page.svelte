<script lang="ts">
  import { onMount } from 'svelte';
  import { readConfig, patchConfig } from '#lib/api/index.ts';
  import type { ConfigRow } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';

  // Levels (EF-UX-009): at most 15 fields on the essential screen.
  const ESSENTIAL_KEYS = new Set([
    'public_url',
    'source.check_mode',
    'instance.jurisdiction',
    'maintenance.window'
  ]);
  const ADVANCED_KEYS = new Set([
    'export.missing_floor',
    'export.missing_ratio',
    'export.missing_ceiling'
  ]);

  let items = $state<ConfigRow[]>([]);
  let failed = $state(false);
  let message = $state('');
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  function levelOf(key: string): 'essential' | 'advanced' | 'expert' {
    if (ESSENTIAL_KEYS.has(key)) return 'essential';
    if (ADVANCED_KEYS.has(key)) return 'advanced';
    return 'expert';
  }

  function isSecret(key: string): boolean {
    return key === 'notify.urls';
  }

  function isGreyed(row: ConfigRow): boolean {
    return !!row.overridden_by || row.source === 'env' || row.source === 'fixed' || row.source === 'toml';
  }

  function originOf(row: ConfigRow): string {
    return row.overridden_by ?? row.source;
  }

  async function load(): Promise<void> {
    try {
      const { data } = await readConfig({ throwOnError: true });
      items = data.items;
    } catch {
      failed = true;
      message = 'Erreur de chargement.';
    }
  }

  onMount(load);

  async function save(key: string, value: string): Promise<void> {
    failed = false;
    message = '';
    const again = async () => {
      try {
        const { data } = await patchConfig({
          headers: headers(),
          body: { values: { [key]: value } },
          throwOnError: true
        });
        items = data.items;
      } catch (error) {
        if (!needsReauth(error)) {
          failed = true;
          message = 'Erreur de chargement.';
        }
      }
    };
    try {
      await again();
    } catch (error) {
      if (needsReauth(error)) {
        retry = again;
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        failed = true;
        message = 'Erreur de chargement.';
      }
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
    const g: Record<'essential' | 'advanced' | 'expert', ConfigRow[]> = {
      essential: [],
      advanced: [],
      expert: []
    };
    for (const row of items) g[levelOf(row.key)].push(row);
    return g;
  });
</script>

<svelte:head>
  <title>Réglages</title>
</svelte:head>

<main>
  <h1>Réglages</h1>

  {#each (['essential', 'advanced', 'expert'] as const) as level (level)}
    <section data-level={level} aria-labelledby={`level-${level}`}>
      <h2 id={`level-${level}`}>
        {level === 'essential' ? 'Essentiel' : level === 'advanced' ? 'Avancé' : 'Expert'}
      </h2>
      {#each groups[level] as row (row.key)}
        <div class="field" data-testid="setting-field" data-key={row.key}>
          <label for={row.key}>{row.key}</label>
          {#if isSecret(row.key)}
            <input
              id={row.key}
              type="password"
              value=""
              placeholder="••••••••"
              disabled
              aria-disabled="true"
            />
          {:else}
            <input
              id={row.key}
              type="text"
              value={String(row.value ?? '')}
              disabled={isGreyed(row)}
              aria-disabled={isGreyed(row)}
              onchange={(e) => save(row.key, (e.target as HTMLInputElement).value)}
            />
          {/if}
          {#if isGreyed(row)}
            <span class="origin" data-origin={originOf(row)}>
              Origine : {originOf(row)}
            </span>
          {/if}
          {#if row.warning}
            <span class="warn">{row.warning}</span>
          {/if}
        </div>
      {/each}
    </section>
  {/each}

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
    background: var(--color-surface, #eee);
  }
  .origin {
    color: var(--color-muted, #666);
    font-size: 0.875rem;
  }
  .err {
    color: var(--color-err, #b00);
  }
</style>