<script lang="ts">
  // "Mes demandes" (EF-UX-010): one state per request, in the family's words, and the link that
  // opens Kavita when it is there. A request that was refused, not found or withdrawn says so, and
  // gives the reason the admin wrote.
  import type { RequestOut } from '#lib/api/types.gen.ts';
  import OpenInKavita from '#lib/components/OpenInKavita.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';

  let { request }: { request: RequestOut } = $props();

  const WORDS: Record<string, MessageKey> = {
    preparing: 'activity.state.preparing',
    available: 'activity.state.available',
    waiting: 'activity.state.waiting',
    rejected: 'request.state.rejected',
    not_found: 'request.state.not_found',
    cancelled: 'request.state.cancelled'
  };
  const word = $derived(WORDS[request.family_state ?? request.state]);
</script>

<article class="request-item">
  {#if request.title}
    <span class="title">{request.title}</span>
  {/if}
  {#if word}
    <span class="state">{t(word)}</span>
  {/if}
  {#if request.family_state === 'available'}
    <OpenInKavita url={request.open_url} />
  {/if}
  {#if request.reject_reason}
    <span class="reason">{t('request.reason', { reason: request.reject_reason })}</span>
  {/if}
</article>

<style>
  .request-item {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule, #eee);
  }
  .title {
    font-weight: 600;
  }
  .state {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--color-rule, #ccc);
  }
  .reason {
    font-size: 0.875rem;
    color: var(--color-muted);
  }
</style>
