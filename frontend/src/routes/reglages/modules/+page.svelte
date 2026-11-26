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
  import { hasMessage, t, tOr } from '#lib/i18n/index.ts';
  import { formatDateTime } from '#lib/i18n/format.ts';
  import TrustCard from '#lib/components/TrustCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { Reauth } from '#lib/guard.svelte.ts';
  import { isForbidden } from '#lib/triage.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import ConfirmDialog from '#lib/ui/molecules/ConfirmDialog.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

  let repos = $state<RepoOut[]>([]);
  let plugins = $state<PluginOut[]>([]);
  let attestations = $state<Record<string, AttestationOut[]>>({});
  let adminOnly = $state(false);
  let loaded = $state(false);
  // an action that cannot be undone waits here for its confirmation
  let confirming = $state<{ title: string; message: string; label: string; run: () => Promise<void> } | null>(null);
  let notice = $state<{ text: string; kind: 'status' | 'alert' } | null>(null);
  let busy = $state(false);
  const reauth = new Reauth();

  let addUrl = $state('');
  let addPubkey = $state('');

  let attestKey = $state('');
  let attestField = $state('content_trust');
  let attestValue = $state('publisher');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  function say(text: string, kind: 'status' | 'alert'): void {
    notice = { text, kind };
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
      if (!plugin.key) continue; // nothing loadable: the attestations are addressed by the key
      const { data } = await readAttestations({ path: { key: plugin.key }, throwOnError: false });
      next[plugin.key] = data?.items ?? [];
    }
    attestations = next;
  }

  onMount(async () => {
    await load();
    loaded = true;
  });

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
          if (reauth.askAgain(error, call)) return;
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
          if (reauth.askAgain(error, call)) return;
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
          if (reauth.askAgain(error, call)) return;
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

  /** The lifecycle and attestation routes are addressed by the plugin key, never by the name of
   * its distribution. A plugin without a key is not offered these actions (the buttons are off). */
  const ask = (next: NonNullable<typeof confirming>): void => {
    confirming = next;
  };
  const lifecycleKey = (plugin: PluginOut): string => plugin.key ?? '';

  async function pin(plugin: PluginOut, pinned: boolean): Promise<void> {
    busy = true;
    try {
      const fn = pinned ? pinPlugin : unpinPlugin;
      const { data, error } = await fn({ path: { name: lifecycleKey(plugin) }, headers: headers(), throwOnError: false });
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
      const { data, error } = await updatePlugin({ path: { name: lifecycleKey(plugin) }, headers: headers(), throwOnError: false });
      if (error) {
        say(t('modules.action_refused'), 'alert');
        return;
      }
      if (data) plugins = plugins.map((p) => (p.name === plugin.name ? data : p));
      await loadAttestations(lifecycleKey(plugin));
    } finally {
      busy = false;
    }
  }

  async function rollback(plugin: PluginOut): Promise<void> {
    busy = true;
    const call = async (): Promise<void> => {
      try {
        const { data, error } = await rollbackPlugin({ path: { name: lifecycleKey(plugin) }, headers: headers(), throwOnError: false });
        if (error) {
          if (reauth.askAgain(error, call)) return;
          say(t('modules.action_refused'), 'alert');
          return;
        }
        if (data) plugins = plugins.map((p) => (p.name === plugin.name ? data : p));
        await loadAttestations(lifecycleKey(plugin));
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
          if (reauth.askAgain(error, call)) return;
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
          if (reauth.askAgain(error, call)) return;
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
    const atts = attestations[plugin.key ?? ''] ?? [];
    const publisher = atts.some((a) => a.field === 'content_trust' && a.value === 'publisher');
    const level: 'official' | 'community' | 'unknown' = publisher ? 'community' : 'unknown';
    const lines: TrustLineOut[] = [
      { level: 'official', label: t('trust.level.official'), current: String(level) === 'official' },
      { level: 'community', label: t('trust.level.community'), current: String(level) === 'community' },
      { level: 'unknown', label: t('trust.level.unknown'), current: String(level) === 'unknown' }
    ];
    return { level, lines };
  }
</script>

<svelte:head>
  <title>{t('modules.title')}</title>
</svelte:head>

<main class="page">
  <PageHead
    title={t('modules.title')}
    crumbs={[{ label: t('settings.title'), href: '/reglages' }, { label: t('modules.title') }]}
    lede={t('modules.lede')}
  />

  {#if adminOnly}
    <Alert tone="danger" title={t('screens.admin_only')} live="assertive" />
  {:else if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('modules.loading')}</p>
      <Skeleton shape="block" /><Skeleton /><Skeleton short />
    </div>
  {:else}
    <section class="section" aria-labelledby="indexes">
      <div class="section-head"><h2 id="indexes">{t('modules.indexes')}</h2></div>
      {#if repos.length === 0}
        <EmptyState title={t('modules.no_indexes')} headingLevel={3} art={false} />
      {:else}
        <ul class="cards">
          {#each repos as repo (repo.url)}
            <li class="card">
              <div class="card__head">
                <span class="url">{repo.url}</span>
                <span class="btn-row">
                  <Button size="sm" icon="refresh" onclick={() => refreshRepo(repo.url)} disabled={busy}>{t('modules.refresh')}</Button>
                  <Button
                    size="sm"
                    variant="danger-ghost"
                    onclick={() => ask({
                      title: t('modules.remove_index_title', { url: repo.url }),
                      message: t('modules.remove_index_message'),
                      label: t('modules.remove_index_confirm'),
                      run: () => removeRepo(repo.url)
                    })}
                    disabled={busy}>{t('modules.remove_index')}</Button
                  >
                </span>
              </div>
              <dl class="meta-list">
                <dt>{t('modules.fingerprint')}</dt>
                <dd class="mono">{repo.key_id}</dd>
                <dt>{t('modules.version')}</dt>
                <dd class="num">{repo.last_version}</dd>
                <dt>{t('modules.expires')}</dt>
                <dd>{repo.expires ? formatDateTime(repo.expires) : t('modules.no_expiry')}</dd>
              </dl>
            </li>
          {/each}
        </ul>
      {/if}

      <form class="panel form" aria-label={t('modules.add_index')} onsubmit={(e) => { e.preventDefault(); void addRepo(); }}>
        <Field label={t('modules.index_url')}>
          {#snippet children(control)}<Input {...control} mono bind:value={addUrl} />{/snippet}
        </Field>
        <Field label={t('modules.index_pubkey')}>
          {#snippet children(control)}<Input {...control} mono bind:value={addPubkey} />{/snippet}
        </Field>
        <div><Button type="submit" variant="primary" icon="plus" loading={busy}>{t('modules.add_index')}</Button></div>
      </form>
    </section>

    <section class="section" aria-labelledby="installed">
      <div class="section-head"><h2 id="installed">{t('modules.installed')}</h2></div>
      {#if plugins.length === 0}
        <EmptyState title={t('modules.no_plugins')} headingLevel={3} art={false} />
      {:else}
        <ul class="cards">
          {#each plugins as plugin (plugin.name)}
            <li class="card">
              <div class="card__head">
                <span class="card__id">
                  <strong class="card__name">{plugin.name}</strong>
                  <span class="num muted">{plugin.version}</span>
                  <Badge tone="neutral">
                    {tOr(`modules.mode.${plugin.mode}`, plugin.mode)}
                    {#if hasMessage(`modules.mode.${plugin.mode}`)}<code class="raw-code">{plugin.mode}</code>{/if}
                  </Badge>
                  <Badge tone={plugin.status === 'enabled' ? 'success' : 'warning'}>
                    {tOr(`modules.status.${plugin.status}`, plugin.status)}
                    {#if hasMessage(`modules.status.${plugin.status}`)}<code class="raw-code">{plugin.status}</code>{/if}
                  </Badge>
                </span>
                <Button
                  size="sm"
                  variant={plugin.status === 'enabled' ? 'secondary' : 'primary'}
                  onclick={() => togglePlugin(plugin, plugin.status !== 'enabled')}
                  disabled={busy}
                >
                  {plugin.status === 'enabled' ? t('modules.disable') : t('modules.enable')}
                </Button>
              </div>
              {#if plugin.reason}<Alert tone="warning" title={plugin.reason} />{/if}
              {#if plugin.notice}<Alert tone="info" title={plugin.notice} />{/if}

              <div class="btn-row">
                <Button size="sm" variant="ghost" onclick={() => pin(plugin, true)} disabled={busy || !plugin.key}>{t('modules.pin')}</Button>
                <Button size="sm" variant="ghost" onclick={() => pin(plugin, false)} disabled={busy || !plugin.key}>{t('modules.unpin')}</Button>
                <Button size="sm" variant="ghost" icon="refresh" onclick={() => update(plugin)} disabled={busy || !plugin.key}>{t('modules.update')}</Button>
                <Button
                  size="sm"
                  variant="danger-ghost"
                  icon="undo"
                  onclick={() => ask({
                    title: t('modules.rollback_title', { name: plugin.name }),
                    message: t('modules.rollback_message'),
                    label: t('modules.rollback_confirm'),
                    run: () => rollback(plugin)
                  })}
                  disabled={busy || !plugin.key}>{t('modules.rollback')}</Button
                >
              </div>

              <TrustCard trust={trustOf(plugin)} />

              <div class="attestations">
                <h3>{t('modules.attestations')}</h3>
                {#if (attestations[plugin.key ?? ''] ?? []).length === 0}
                  <p class="muted">{t('modules.no_attestations')}</p>
                {:else}
                  <ul class="entries">
                    {#each attestations[plugin.key ?? ''] ?? [] as a (a.field + a.value)}
                      <li class="attestation">
                        <span>{a.field}: {a.value}</span>
                        <Button
                          size="sm"
                          variant="danger-ghost"
                          onclick={() => ask({
                            title: t('modules.withdraw_title', { field: a.field }),
                            message: t('modules.withdraw_message'),
                            label: t('modules.withdraw_confirm'),
                            run: () => doWithdraw(lifecycleKey(plugin), a.field)
                          })}
                          disabled={busy}>{t('modules.withdraw')}</Button
                        >
                      </li>
                    {/each}
                  </ul>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      {/if}

      <form class="panel form" aria-label={t('modules.attest')} onsubmit={(e) => { e.preventDefault(); void doAttest(); }}>
        <Field label={t('modules.attest_key')}>
          {#snippet children(control)}<Input {...control} mono bind:value={attestKey} />{/snippet}
        </Field>
        <Field label={t('modules.attest_field')}>
          {#snippet children(control)}
            <Select
              {...control}
              bind:value={attestField}
              options={[
                { value: 'content_trust', label: t('modules.field.content_trust') },
                { value: 'access_partitioning', label: t('modules.field.access_partitioning') },
                { value: 'egress', label: t('modules.field.egress') }
              ]}
            />
          {/snippet}
        </Field>
        <Field label={t('modules.attest_value')}>
          {#snippet children(control)}<Input {...control} bind:value={attestValue} />{/snippet}
        </Field>
        <div><Button type="submit" variant="primary" loading={busy}>{t('modules.attest')}</Button></div>
      </form>
    </section>
  {/if}

  {#if reauth.reauthMethods}
    <ReauthPrompt methods={reauth.reauthMethods} ondone={() => reauth.afterReauth()} oncancel={() => reauth.cancelReauth()} />
  {/if}

  {#if notice}
    <Alert
      tone={notice.kind === 'alert' ? 'danger' : 'success'}
      live={notice.kind === 'alert' ? 'assertive' : 'polite'}
      title={notice.text}
    />
  {/if}
</main>

<ConfirmDialog
  bind:open={() => confirming !== null, (value) => { if (!value) confirming = null; }}
  title={confirming?.title ?? ''}
  message={confirming?.message}
  confirmLabel={confirming?.label ?? ''}
  onconfirm={async () => {
    const pending = confirming;
    confirming = null;
    await pending?.run();
  }}
/>

<style>
  .cards {
    list-style: none;
    margin: 0 0 var(--space-5);
    padding: 0;
    display: grid;
    gap: var(--space-4);
  }
  .card {
    display: grid;
    gap: var(--space-3);
    padding: var(--space-4) var(--space-5);
    border: var(--rule-hair) solid var(--color-rule);
    border-radius: var(--radius-md);
    background: var(--color-surface);
  }
  .card__head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }
  .card__id {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-2) var(--space-3);
    min-width: 0;
  }
  .card__name {
    font: 500 var(--step-1) / var(--leading-tight) var(--font-text);
    overflow-wrap: anywhere;
  }
  .url,
  .mono {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--step--1);
    overflow-wrap: anywhere;
  }
  .attestations h3 {
    margin: 0 0 var(--space-2);
    font-size: var(--step-0);
  }
  .attestation {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding: var(--space-2) 0;
  }
  .form {
    display: grid;
    gap: var(--space-4);
    max-width: var(--form-max);
    margin-top: var(--space-5);
  }
  .raw-code {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: 0.85em;
    opacity: 0.8;
  }
</style>
