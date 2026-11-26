<script lang="ts">
  // The trackers of a profile (M6 L13, L15, EF-INT-010): link, unlink, push opt-in. A pull is
  // only a hint and never writes a read mark. The server is the gate: a child links only where
  // the admin allowed it, and a link is the profile's own.
  import { onMount } from 'svelte';
  import { link, listLinks, me, setPush, unlink } from '#lib/api/index.ts';
  import type { LinkItem } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Switch from '#lib/ui/atoms/Switch.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import ErrorState from '#lib/ui/molecules/ErrorState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import { formatDateTime } from '#lib/i18n/format.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { Reauth } from '#lib/guard.svelte.ts';
  import { isForbidden, problemOf } from '#lib/triage.ts';

  let userId = $state('');
  let role = $state('');
  let links = $state<LinkItem[]>([]);
  let notAllowed = $state(false);
  let failed = $state(false);
  let notice = $state<{ text: string; kind: 'status' | 'alert' } | null>(null);
  let busy = $state(false);
  let loaded = $state(false);
  const reauth = new Reauth();

  let pluginKey = $state('');
  let token = $state('');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  function say(text: string, kind: 'status' | 'alert'): void {
    notice = { text, kind };
  }

  async function load(): Promise<void> {
    try {
      await read();
    } finally {
      loaded = true;
    }
  }

  async function read(): Promise<void> {
    failed = false;
    notice = null;
    try {
      const { data: meData } = await me({ throwOnError: true });
      userId = meData.id;
      role = meData.role;
    } catch {
      failed = true;
      say(t('trackers.error'), 'alert');
      return;
    }
    const { data, error } = await listLinks({ path: { user_id: userId }, throwOnError: false });
    if (error) {
      if (isForbidden(error)) {
        if (role === 'child') {
          notAllowed = true;
        } else {
          say(t('screens.admin_only'), 'alert');
        }
      } else {
        say(t('trackers.error'), 'alert');
      }
      return;
    }
    links = data?.items ?? [];
  }

  onMount(load);

  async function doLink(): Promise<void> {
    busy = true;
    const call = async (): Promise<void> => {
      try {
        const { data, error } = await link({
          path: { user_id: userId, plugin_key: pluginKey },
          headers: headers(),
          body: { token },
          throwOnError: false
        });
        if (error) {
          if (reauth.askAgain(error, call)) return;
          const code = problemOf(error).code;
          if (code === 'tracker_child_refused') say(t('trackers.child_refused'), 'alert');
          else say(t('trackers.refused'), 'alert');
          return;
        }
        if (data) {
          const item: LinkItem = {
            plugin_key: data.plugin_key,
            linked_at: data.linked_at,
            push_enabled: data.push_enabled
          };
          links = [...links.filter((l) => l.plugin_key !== data.plugin_key), item];
        }
        token = '';
        say(t('trackers.linked'), 'status');
      } finally {
        busy = false;
      }
    };
    await call();
  }

  async function doUnlink(key: string): Promise<void> {
    busy = true;
    const call = async (): Promise<void> => {
      try {
        const { error } = await unlink({ path: { user_id: userId, plugin_key: key }, headers: headers(), throwOnError: false });
        if (error) {
          if (reauth.askAgain(error, call)) return;
          say(t('trackers.refused'), 'alert');
          return;
        }
        links = links.filter((l) => l.plugin_key !== key);
        say(t('trackers.unlinked'), 'status');
      } finally {
        busy = false;
      }
    };
    await call();
  }

  async function togglePush(linkItem: LinkItem, enabled: boolean): Promise<void> {
    busy = true;
    const call = async (): Promise<void> => {
      try {
        const { data, error } = await setPush({
          path: { user_id: userId, plugin_key: linkItem.plugin_key },
          headers: headers(),
          body: { enabled },
          throwOnError: false
        });
        if (error) {
          if (reauth.askAgain(error, call)) return;
          say(t('trackers.push_refused'), 'alert');
          return;
        }
        if (data) {
          links = links.map((l) =>
            l.plugin_key === linkItem.plugin_key ? { ...l, push_enabled: data.push_enabled } : l
          );
        }
      } finally {
        busy = false;
      }
    };
    await call();
  }
</script>

<svelte:head>
  <title>{t('trackers.title')}</title>
</svelte:head>

<main class="page page--form">
  <div class="running-head"><span>{t('nav.settings')}</span></div>
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('trackers.title')}</h1>
      {#if !notAllowed}<p class="lede">{t('trackers.lede')}</p>{/if}
    </div>
  </div>

  {#if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('trackers.loading')}</p>
      <Skeleton /><Skeleton short />
    </div>
  {:else if notAllowed}
    <div role="alert">
      <ErrorState kind="forbidden" title={t('trackers.child_title')} headingLevel={2}>
        {t('trackers.child_refused')}
      </ErrorState>
    </div>
  {:else if failed}
    <div role="alert">
      <ErrorState kind="server" title={t('trackers.error_title')} headingLevel={2}>
        {t('trackers.error')}
        {#snippet actions()}<Button icon="refresh" onclick={() => void load()}>{t('error.retry')}</Button>{/snippet}
      </ErrorState>
    </div>
  {:else}
    <section class="section" aria-labelledby="linked">
      <div class="section-head"><h2 id="linked">{t('trackers.linked_list')}</h2></div>
      {#if links.length === 0}
        <EmptyState title={t('trackers.none')} headingLevel={3} art={false} />
      {:else}
        <ul class="entries" role="list">
          {#each links as l (l.plugin_key)}
            <li class="tracker">
              <div class="tracker__text">
                <p class="tracker__name">{l.plugin_key}</p>
                <p class="hint">{t('trackers.linked_at')} {formatDateTime(l.linked_at)}</p>
              </div>
              <Switch
                label={t('trackers.push_opt_in')}
                checked={l.push_enabled}
                disabled={busy}
                onchange={(e) => togglePush(l, (e.target as HTMLInputElement).checked)}
              />
              <Button size="sm" variant="danger-ghost" icon="x" disabled={busy} onclick={() => doUnlink(l.plugin_key)}>{t('trackers.unlink')}</Button>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <section class="section" aria-labelledby="link">
      <div class="section-head"><h2 id="link">{t('trackers.link')}</h2></div>
      <form class="new" onsubmit={(e) => { e.preventDefault(); void doLink(); }}>
        <Field label={t('trackers.plugin_key')}>
          {#snippet children(control)}<Input {...control} bind:value={pluginKey} autocomplete="off" />{/snippet}
        </Field>
        <Field label={t('trackers.token')} hint={t('trackers.link_help')}>
          {#snippet children(control)}<Input {...control} type="password" bind:value={token} autocomplete="off" />{/snippet}
        </Field>
        <div class="btn-row">
          <Button type="submit" variant="primary" icon="plus" loading={busy}>{t('trackers.link_action')}</Button>
        </div>
      </form>
    </section>
  {/if}

  {#if reauth.reauthMethods}
    <ReauthPrompt methods={reauth.reauthMethods} ondone={() => reauth.afterReauth()} oncancel={() => reauth.cancelReauth()} />
  {/if}

  {#if notice && loaded && !failed && !notAllowed}
    <div class="notice">
      <Alert
        tone={notice.kind === 'alert' ? 'danger' : 'success'}
        title={notice.text}
        live={notice.kind === 'alert' ? 'assertive' : 'polite'}
      />
    </div>
  {/if}
</main>

<style>
  .tracker {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: center;
    gap: var(--space-2) var(--space-4);
    padding-block: var(--space-3);
  }
  .tracker p {
    margin: 0;
  }
  .tracker__name {
    font: 600 var(--step-1) / var(--leading-tight) var(--font-text);
    overflow-wrap: anywhere;
  }
  .new {
    display: grid;
    gap: var(--space-4);
  }
  .notice {
    margin-top: var(--space-5);
  }
  @media (max-width: 40rem) {
    .tracker {
      grid-template-columns: minmax(0, 1fr);
      justify-items: start;
    }
  }
</style>
