<script lang="ts">
  // « Soutenir l'auteur » (EF-BIB-032, ADR-0047). The server is the only source of the links: it
  // has cleaned them (https only, never affiliated) and says which provider gave each one and
  // when. This screen shows what it was told and trusts nothing more: a URL that is not plain
  // https is not drawn at all, and every link opens in a new tab with rel="noopener noreferrer".
  // Pausing a community binding is only proposed here; the admin decides with one explicit click.
  import { supportAuthorPause, supportAuthorRead } from '#lib/api/index.ts';
  import type { SupportAuthorOut } from '#lib/api/types.gen.ts';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import { formatDateTime, t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';

  let { editionId }: { editionId: string } = $props();

  type Link = SupportAuthorOut['links'][number];
  const KINDS: Record<Link['kind'], MessageKey> = {
    official_read: 'support.kind.official_read',
    buy: 'support.kind.buy',
    licensed_in: 'support.kind.licensed_in'
  };

  let loaded = $state<SupportAuthorOut | null>(null);
  let problem = $state<ProblemView | null>(null);
  let paused = $state(false);

  function isHttps(url: string): boolean {
    try {
      return new URL(url).protocol === 'https:';
    } catch {
      return false;
    }
  }

  const links = $derived((loaded?.links ?? []).filter((link) => isHttps(link.url)));
  const proposed = $derived(loaded?.pause_suggestions[0] ?? null);

  async function load(): Promise<void> {
    const { data } = await supportAuthorRead({ path: { edition_id: editionId } });
    // An Edition she may not see answers like an absent one: nothing is drawn, no error either.
    loaded = data ?? null;
  }

  async function pause(bindingId: string): Promise<void> {
    const { error } = await supportAuthorPause({
      headers: { 'X-CSRF-Token': csrfToken() },
      path: { edition_id: editionId, binding_id: bindingId }
    });
    if (error !== undefined) {
      problem = problemView(error);
      return;
    }
    problem = null;
    paused = true;
    await load();
  }

  $effect(() => {
    void editionId;
    paused = false;
    void load();
  });
</script>

{#if loaded !== null && (links.length > 0 || proposed !== null || paused)}
  <section class="section" aria-labelledby="support-author-title">
    <div class="section-head"><h2 id="support-author-title">{t('support.title')}</h2></div>
    {#if links.length > 0}
      <p class="hint">{t('support.intro')}</p>
      <ul class="entries support">
        {#each links as link (`${link.provider}|${link.url}`)}
          <li>
            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- a third-party address, kept only when it is plain https -->
            <a href={link.url} target="_blank" rel="noopener noreferrer"
              >{t('support.link', { kind: t(KINDS[link.kind]), provider: link.provider })}</a
            >
            <small class="hint">
              {t('support.provenance', {
                provider: link.provider,
                date: link.fetched_at ? formatDateTime(link.fetched_at) : '—'
              })}
            </small>
            {#if link.lang !== 'und'}
              <small class="hint">{t('support.lang', { lang: link.lang })}</small>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if proposed !== null}
      <div class="stack pause">
        <p>{t('support.pause.hint')}</p>
        <Button variant="secondary" onclick={() => pause(proposed)}>{t('support.pause.action')}</Button>
      </div>
    {/if}
    {#if paused}
      <Alert tone="success" live="polite" title={t('support.pause.done')} />
    {/if}
    {#if problem}
      <ProblemAlert {problem} />
    {/if}
  </section>
{/if}

<style>
  .support > li {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-1) var(--space-4);
    min-height: var(--target);
    padding: var(--space-2) 0;
    overflow-wrap: anywhere;
  }
  .pause {
    margin-top: var(--space-4);
    max-width: var(--measure);
  }
</style>
