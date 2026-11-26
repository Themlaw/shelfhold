<script lang="ts">
  import { onMount } from 'svelte';
  import { oidcIdentities, oidcLink, oidcUnlink } from '#lib/api/index.ts';
  import type { OidcIdentityItem, OidcLastUnlink } from '#lib/api/types.gen.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { formatDate } from '#lib/i18n/format.ts';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

  const INTENT = 'oidc-intent';
  // what a person asked for before the provider took the browser away is worth no more than the
  // flow that carried it: ten minutes
  const INTENT_TTL_MS = 10 * 60 * 1000;
  type Intent = 'link' | 'unlink';

  let items = $state<OidcIdentityItem[]>([]);
  let issuer = $state<string | null>(null);
  let loaded = $state(false);
  let lastUnlinked = $state<OidcLastUnlink | null>(null);
  // proved at the provider, not yet done: the person says it again (a page restored from the
  // history must never act on a request made earlier)
  let resumable = $state<Intent | null>(null);
  let message = $state('');
  let failed = $state(false);
  let busy = $state(false);
  let reauthMethods = $state<string[] | null>(null);
  let pending: Intent | null = null;

  const params = typeof window === 'undefined' ? null : new URLSearchParams(window.location.search);

  async function load(): Promise<void> {
    try {
      const { data } = await oidcIdentities({ throwOnError: true });
      items = data.items;
      issuer = data.configured_issuer;
      lastUnlinked = data.last_unlinked ?? null;
    } catch {
      failed = true;
      message = t('oidc.list_error');
    } finally {
      loaded = true;
    }
  }

  /** What was asked before the provider took the browser away: taken up once it is back. */
  const takeIntent = (): Intent | null => {
    const raw = sessionStorage.getItem(INTENT);
    sessionStorage.removeItem(INTENT);
    try {
      const noted = JSON.parse(raw ?? 'null') as { intent?: string; at?: number } | null;
      const fresh = typeof noted?.at === 'number' && Date.now() - noted.at < INTENT_TTL_MS;
      return fresh && (noted?.intent === 'link' || noted?.intent === 'unlink') ? noted.intent : null;
    } catch {
      return null;
    }
  };

  onMount(() => {
    // the confirmation page of a link, and the provider's answer to a proof, send the person back
    // here with the result in the address
    const result = params?.get('oidc');
    const intent = takeIntent();
    if (result === 'linked') message = t('oidc.linked');
    if (result === 'reauth') message = t('oidc.reauth_done');
    if (result === 'refused') {
      failed = true;
      message = t('oidc.refused');
    }
    // only a proof the provider gave offers to go on with what the person had asked for
    if (result === 'reauth' && intent) resumable = intent;
    return load();
  });

  /** A stale session asks for a proof in place (EF-USR-022); a session the provider opened is
   * sent back to it (`prompt=login`, `max_age`) and the action is taken up on return. */
  async function run(intent: Intent): Promise<void> {
    busy = true;
    failed = false;
    message = '';
    pending = intent;
    try {
      if (intent === 'link') {
        const { data } = await oidcLink({
          headers: { 'X-CSRF-Token': csrfToken() },
          throwOnError: true
        });
        window.location.assign(data.authorization_url);
      } else {
        await oidcUnlink({ headers: { 'X-CSRF-Token': csrfToken() }, throwOnError: true });
        message = t('oidc.unlinked');
        await load();
      }
      reauthMethods = null;
    } catch (error) {
      if (needsReauth(error)) {
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        failed = true;
        message = t(intent === 'link' ? 'oidc.error' : 'oidc.unlink_error');
      }
    } finally {
      busy = false;
    }
  }

  const link = () => run('link');
  const unlink = () => run('unlink');

  const resume = async (): Promise<void> => {
    const intent = resumable;
    resumable = null;
    if (intent) await run(intent);
  };

  async function afterReauth(): Promise<void> {
    reauthMethods = null;
    if (pending) await run(pending);
  }
</script>

<svelte:head>
  <title>{t('oidc.title')}</title>
</svelte:head>

<main class="page">
  <PageHead
    title={t('oidc.title')}
    crumbs={[{ label: t('oidc.back'), href: '/reglages' }, { label: t('oidc.title') }]}
  />

  <div class="stack page--form">
    {#if !loaded}
      <div aria-busy="true"><Skeleton /><Skeleton short /></div>
    {:else if issuer === null && items.length === 0}
      <p>{t('oidc.not_configured')}</p>
    {:else if issuer !== null}
      <p>{t('oidc.issuer', { issuer })}</p>
    {/if}

    {#if issuer === null && items.length === 0}
      <!-- nothing to link, nothing to remove -->
    {:else if items.length === 0}
      <p>{t('oidc.none')}</p>
    {:else}
      <ul class="identities">
        {#each items as item (item.issuer)}
          <li>
            <span class="issuer">{item.issuer}</span>
            <span class="muted">
              {t('oidc.linked_since', { date: formatDate(item.linked_at) })}
              {#if item.linked_by_other}· {t('oidc.by_admin')}{/if}
            </span>
            <Button variant="secondary" disabled={busy} onclick={unlink}>{t('oidc.unlink')}</Button>
          </li>
        {/each}
      </ul>
    {/if}

    {#if items.length === 0 && issuer !== null}
      <div class="btn-row">
        <Button variant="primary" disabled={busy} onclick={link}>{t('oidc.link')}</Button>
      </div>
    {/if}

    {#if resumable}
      <p>{t(resumable === 'unlink' ? 'oidc.resume_unlink' : 'oidc.resume_link')}</p>
      <div class="btn-row">
        <Button variant="primary" disabled={busy} onclick={resume}>
          {t(resumable === 'unlink' ? 'oidc.unlink' : 'oidc.link')}
        </Button>
      </div>
    {/if}

    {#if lastUnlinked}
      <p class="muted">
        {t('oidc.last_unlinked', { date: formatDate(lastUnlinked.at) })}
        {#if lastUnlinked.by_other}· {t('oidc.by_admin_removed')}{/if}
      </p>
    {/if}

    {#if reauthMethods}
      <ReauthPrompt
        methods={reauthMethods}
        ondone={afterReauth}
        oncancel={() => (reauthMethods = null)}
        onprovider={() =>
          sessionStorage.setItem(INTENT, JSON.stringify({ intent: pending ?? 'link', at: Date.now() }))}
      />
    {/if}

    {#if message}
      <Alert
        tone={failed ? 'danger' : 'success'}
        live={failed ? 'assertive' : 'polite'}
        title={message}
      />
    {/if}
  </div>
</main>

<style>
  .identities {
    list-style: none;
    margin: 0;
    padding: 0;
    border-top: var(--rule-hair) solid var(--color-rule);
  }
  .identities li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2) var(--space-4);
    padding-block: var(--space-3);
    border-bottom: var(--rule-hair) solid var(--color-rule);
  }
  .issuer {
    overflow-wrap: anywhere;
    font-weight: 600;
  }
  .muted {
    color: var(--color-muted);
  }
</style>
