<script lang="ts">
  // The modules screen (M6 L15, EF-SRC-017, EF-SRC-020, EF-UX-011): signed indexes, the
  // installed modules, their activation, their attestations and their trust card, then the
  // lifecycle (update, pin, rollback). The server is the gate for every reserved action.
  import { onMount } from 'svelte';
  import {
    addPluginRepo,
    attest,
    disablePlugin,
    enablePlugin,
    pinPlugin,
    readAttestations,
    readPluginRepos,
    readPlugins,
    refreshPluginRepo,
    removePluginRepo,
    rollbackPlugin,
    unpinPlugin,
    updatePlugin,
    withdraw
  } from '#lib/api/index.ts';
  import type {
    AttestationOut,
    PluginOut,
    RepoOut,
    TrustCardOut,
    TrustLineOut
  } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import { formatDateTime } from '#lib/i18n/format.ts';
  import TrustCard from '#lib/components/TrustCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { isForbidden, needsReauth, problemOf } from '#lib/triage.ts';

  let repos = $state<RepoOut[]>([]);
  let plugins = $state<PluginOut[]>([]);
  let attestations = $state<Record<string, AttestationOut[]>>({});
  let adminOnly = $state(false);
  let notice = $state<{ text: string; kind: 'status' | 'alert' } | null>(null);
  let busy = $state(false);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  let addUrl = $state('');
  let addPubkey = $state('');

  let attestKey = $state('');
  let attestField = $state('content_trust');
  let attestValue = $state('publisher');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  function say(text: string, kind: 'status' | 'alert'): void {
    notice = { text, kind };
  }

  function askAgain(error: unknown, replay: () => Promise<void>): boolean {
    if (!needsReauth(error)) return false;
    retry = replay;
    reauthMethods = problemOf(error).reauth_methods ?? ['password'];
    return true;
  }

  async function load(): Promise<void> {
    notice = null;
    const { data: reposData, error: reposError } = await readPluginRepos({ throwOnError: false });
    if (reposError) {
      adminOnly = isForbidden(reposError);
      say(adminOnly ? t('screens.admin_only') : t('modules.error'), 'alert');
      return;
    }
    repos = reposData?.items ?? [];
    const { data: pluginsData, error: pluginsError } = await readPlugins({ throwOnError: false });
    if (pluginsError) {
      adminOnly = isForbidden(pluginsError);
      say(adminOnly ? t('screens.admin_only') : t('modules.error'), 'alert');
      return;
    }
    plugins = pluginsData?.items ?? [];
    const next: Record<string, AttestationOut[]> = {};
    for (const plugin of plugins) {
      const { data } = await readAttestations({ path: { key: plugin.name }, throwOnError: false });
      next[plugin.name] = data?.items ?? [];
    }
    attestations = next;
  }

  onMount(load);

  async function addRepo(): Promise<void> {
    busy = true;
    const call = async (): Promise<void> => {
      try {
        const { data, error } = await addPluginRepo({
          headers: headers(),
          body: { url: addUrl, pubkey: addPubkey || null },
          throwOnError: false
        });
        if (error) {
          if (askAgain(error, call)) return;
          say(t('modules.index_refused'), 'alert');
          return;
        }
        if (data) repos = [...repos, data];
        addUrl = '';
        addPubkey = '';
        say(t('modules.index_added'), 'status');
      } finally {
        busy = false;
      }
    };
    await call();
  }

  async function removeRepo(url: string): Promise<void> {
    busy = true;
    const call = async (): Promise<void> => {
      try {
        const { error } = await removePluginRepo({ path: { url }, headers: headers(), throwOnError: false });
        if (error) {
          if (askAgain(error, call)) return;
          say(t('modules.index_refused'), 'alert');
          return;
        }
        repos = repos.filter((r) => r.url !== url);
        say(t('modules.index_removed'), 'status');
      } finally {
        busy = false;
      }
    };
    await call();
  }

  async function refreshRepo(url: string): Promise<void> {
    busy = true;
    try {
      const { data, error } = await refreshPluginRepo({ path: { url }, headers: headers(), throwOnError: false });
      if (error) {
        say(t('modules.index_refused'), 'alert');
        return;
      }
      if (data) {
        repos = repos.map((r) =>
          r.url === url ? { ...r, last_version: data.version } : r
        );
        say(t('modules.index_refreshed'), 'status');
      }
    } finally {
      busy = false;
    }
  }

  async function togglePlugin(plugin: PluginOut, enabled: boolean): Promise<void> {
    busy = true;
    const call = async (): Promise<void> => {
      try {
        const fn = enabled ? enablePlugin : disablePlugin;
        const { data, error } = await fn({
          path: { name: plugin.name },
          headers: headers(),
          throwOnError: false
        });
        if (error) {
          if (askAgain(error, call)) return;
          say(t('modules.action_refused'), 'alert');
          return;
        }
        if (data) {
          plugins = plugins.map((p) => (p.name === plugin.name ? data : p));
        }
      } finally {
        busy = false;
      }
    };
    await call();
  }

  async function pin(plugin: PluginOut, pinned: boolean): Promise<void> {
    busy = true;
    try {
      const fn = pinned ? pinPlugin : unpinPlugin;
      const { data, error } = await fn({ path: { name: plugin.name }, headers: headers(), throwOnError: false });
      if (error) {
        say(t('modules.action_refused'), 'alert');
        return;
      }
      if (data) plugins = plugins.map((p) => (p.name === plugin.name ? data : p));
    } finally {
      busy = false;
    }
  }

  async function update(plugin: PluginOut): Promise<void> {
    busy = true;
    try {
      const { data, error } = await updatePlugin({ path: { name: plugin.name }, headers: headers(), throwOnError: false });
      if (error) {
        say(t('modules.action_refused'), 'alert');
        return;
      }
      if (data) plugins = plugins.map((p) => (p.name === plugin.name ? data : p));
      await loadAttestations(plugin.name);
    } finally {
      busy = false;
    }
  }

  async function rollback(plugin: PluginOut): Promise<void> {
    busy = true;
    const call = async (): Promise<void> => {
      try {
        const { data, error } = await rollbackPlugin({ path: { name: plugin.name }, headers: headers(), throwOnError: false });
        if (error) {
          if (askAgain(error, call)) return;
          say(t('modules.action_refused'), 'alert');
          return;
        }
        if (data) plugins = plugins.map((p) => (p.name === plugin.name ? data : p));
        await loadAttestations(plugin.name);
      } finally {
        busy = false;
      }
    };
    await call();
  }

  async function loadAttestations(key: string): Promise<void> {
    const { data } = await readAttestations({ path: { key }, throwOnError: false });
    attestations = { ...attestations, [key]: data?.items ?? [] };
  }

  async function doAttest(): Promise<void> {
    busy = true;
    const call = async (): Promise<void> => {
      try {
        const { data, error } = await attest({
          path: { key: attestKey },
          headers: headers(),
          body: { field: attestField, value: attestValue },
          throwOnError: false
        });
        if (error) {
          if (askAgain(error, call)) return;
          say(t('modules.attestation_refused'), 'alert');
          return;
        }
        if (data) await loadAttestations(attestKey);
        say(t('modules.attested'), 'status');
      } finally {
        busy = false;
      }
    };
    await call();
  }

  async function doWithdraw(key: string, field: string): Promise<void> {
    busy = true;
    const call = async (): Promise<void> => {
      try {
        const { error } = await withdraw({ path: { key, name: field }, headers: headers(), throwOnError: false });
        if (error) {
          if (askAgain(error, call)) return;
          say(t('modules.attestation_refused'), 'alert');
          return;
        }
        await loadAttestations(key);
      } finally {
        busy = false;
      }
    };
    await call();
  }

  function trustOf(plugin: PluginOut): TrustCardOut {
    const atts = attestations[plugin.name] ?? [];
    const publisher = atts.some((a) => a.field === 'content_trust' && a.value === 'publisher');
    const level: 'official' | 'community' | 'unknown' = publisher ? 'community' : 'unknown';
    const lines: TrustLineOut[] = [
      { level: 'official', label: t('trust.level.official'), current: String(level) === 'official' },
      { level: 'community', label: t('trust.level.community'), current: String(level) === 'community' },
      { level: 'unknown', label: t('trust.level.unknown'), current: String(level) === 'unknown' }
    ];
    return { level, lines };
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
</script>

<svelte:head>
  <title>{t('modules.title')}</title>
</svelte:head>

<main class="form-screen">
  <h1>{t('modules.title')}</h1>

  {#if adminOnly}
    <p role="alert">{t('screens.admin_only')}</p>
  {:else}
    <section aria-labelledby="indexes">
      <h2 id="indexes">{t('modules.indexes')}</h2>
      {#if repos.length === 0}
        <p>{t('modules.no_indexes')}</p>
      {:else}
        <ul>
          {#each repos as repo (repo.url)}
            <li>
              <span class="url">{repo.url}</span>
              <span>{t('modules.fingerprint')}: {repo.key_id}</span>
              <span>{t('modules.version')}: {repo.last_version}</span>
              <span>{t('modules.expires')}: {repo.expires ? formatDateTime(repo.expires) : t('modules.no_expiry')}</span>
              <button type="button" onclick={() => refreshRepo(repo.url)} disabled={busy}>{t('modules.refresh')}</button>
              <button type="button" onclick={() => removeRepo(repo.url)} disabled={busy}>{t('modules.remove_index')}</button>
            </li>
          {/each}
        </ul>
      {/if}

      <form onsubmit={(e) => { e.preventDefault(); void addRepo(); }}>
        <label for="index-url">{t('modules.index_url')}</label>
        <input id="index-url" bind:value={addUrl} />
        <label for="index-pubkey">{t('modules.index_pubkey')}</label>
        <input id="index-pubkey" bind:value={addPubkey} />
        <button type="submit" disabled={busy}>{t('modules.add_index')}</button>
      </form>
    </section>

    <section aria-labelledby="installed">
      <h2 id="installed">{t('modules.installed')}</h2>
      {#if plugins.length === 0}
        <p>{t('modules.no_plugins')}</p>
      {:else}
        <ul>
          {#each plugins as plugin (plugin.name)}
            <li>
              <strong>{plugin.name}</strong>
              <span>{plugin.version}</span>
              <span>{plugin.mode}</span>
              <span>{plugin.status}</span>
              {#if plugin.reason}<span>{plugin.reason}</span>{/if}
              {#if plugin.notice}<span>{plugin.notice}</span>{/if}

              <button type="button" onclick={() => togglePlugin(plugin, plugin.status !== 'enabled')} disabled={busy}>
                {plugin.status === 'enabled' ? t('modules.disable') : t('modules.enable')}
              </button>
              <button type="button" onclick={() => pin(plugin, true)} disabled={busy}>{t('modules.pin')}</button>
              <button type="button" onclick={() => pin(plugin, false)} disabled={busy}>{t('modules.unpin')}</button>
              <button type="button" onclick={() => update(plugin)} disabled={busy}>{t('modules.update')}</button>
              <button type="button" onclick={() => rollback(plugin)} disabled={busy}>{t('modules.rollback')}</button>

              <TrustCard trust={trustOf(plugin)} />

              <div class="attestations">
                <h3>{t('modules.attestations')}</h3>
                {#if (attestations[plugin.name] ?? []).length === 0}
                  <p>{t('modules.no_attestations')}</p>
                {:else}
                  <ul>
                    {#each attestations[plugin.name] ?? [] as a (a.field + a.value)}
                      <li>
                        {a.field}: {a.value}
                        <button type="button" onclick={() => doWithdraw(plugin.name, a.field)} disabled={busy}>{t('modules.withdraw')}</button>
                      </li>
                    {/each}
                  </ul>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      {/if}

      <form onsubmit={(e) => { e.preventDefault(); attestKey = attestKey; void doAttest(); }}>
        <label for="attest-key">{t('modules.attest_key')}</label>
        <input id="attest-key" bind:value={attestKey} />
        <label for="attest-field">{t('modules.attest_field')}</label>
        <select id="attest-field" bind:value={attestField}>
          <option value="content_trust">{t('modules.field.content_trust')}</option>
          <option value="access_partitioning">{t('modules.field.access_partitioning')}</option>
          <option value="egress">{t('modules.field.egress')}</option>
        </select>
        <label for="attest-value">{t('modules.attest_value')}</label>
        <input id="attest-value" bind:value={attestValue} />
        <button type="submit" disabled={busy}>{t('modules.attest')}</button>
      </form>
    </section>
  {/if}

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}

  {#if notice}
    <p role={notice.kind === 'alert' ? 'alert' : 'status'}>{notice.text}</p>
  {/if}
</main>

<style>
  main { max-width: 48rem; margin: 0 auto; padding: 2rem 1rem; }
  section { margin: 1.5rem 0; }
  ul { list-style: none; padding: 0; }
  li { margin: 0.75rem 0; padding: 0.5rem; border: 1px solid var(--color-muted, #ccc); border-radius: 0.5rem; }
  .url { font-family: monospace; word-break: break-all; }
  form { display: grid; gap: 0.5rem; margin: 1rem 0; }
  button { margin-right: 0.5rem; }
</style>
