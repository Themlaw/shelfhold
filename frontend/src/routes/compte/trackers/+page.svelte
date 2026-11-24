<script lang="ts">
  // The trackers of a profile (M6 L13, L15, EF-INT-010): link, unlink, push opt-in. A pull is
  // only a hint and never writes a read mark. The server is the gate: a child links only where
  // the admin allowed it, and a link is the profile's own.
  import { onMount } from 'svelte';
  import { link, listLinks, me, setPush, unlink } from '#lib/api/index.ts';
  import type { LinkItem } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
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
  const reauth = new Reauth();

  let pluginKey = $state('');
  let token = $state('');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  function say(text: string, kind: 'status' | 'alert'): void {
    notice = { text, kind };
  }

  async function load(): Promise<void> {
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

<main class="form-screen">
  <h1>{t('trackers.title')}</h1>

  {#if notAllowed}
    <p role="alert">{t('trackers.child_refused')}</p>
  {:else if failed}
    <p role="alert">{t('trackers.error')}</p>
  {:else}
    {#if links.length === 0}
      <p>{t('trackers.none')}</p>
    {:else}
      <ul>
        {#each links as l (l.plugin_key)}
          <li>
            <strong>{l.plugin_key}</strong>
            <span>{t('trackers.linked_at')}: {formatDateTime(l.linked_at)}</span>
            <label>
              <input
                type="checkbox"
                checked={l.push_enabled}
                onchange={(e) => togglePush(l, (e.target as HTMLInputElement).checked)}
                disabled={busy}
              />
              {t('trackers.push_opt_in')}
            </label>
            <button type="button" onclick={() => doUnlink(l.plugin_key)} disabled={busy}>{t('trackers.unlink')}</button>
          </li>
        {/each}
      </ul>
    {/if}

    <section aria-labelledby="link">
      <h2 id="link">{t('trackers.link')}</h2>
      <form onsubmit={(e) => { e.preventDefault(); void doLink(); }}>
        <label for="plugin-key">{t('trackers.plugin_key')}</label>
        <input id="plugin-key" bind:value={pluginKey} />
        <label for="token">{t('trackers.token')}</label>
        <input id="token" type="password" bind:value={token} />
        <button type="submit" disabled={busy}>{t('trackers.link_action')}</button>
      </form>
    </section>
  {/if}

  {#if reauth.reauthMethods}
    <ReauthPrompt methods={reauth.reauthMethods} ondone={() => reauth.afterReauth()} oncancel={() => reauth.cancelReauth()} />
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
  form { display: grid; gap: 0.5rem; margin: 1rem 0; }
  button { margin-right: 0.5rem; }
</style>
