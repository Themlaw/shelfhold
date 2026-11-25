<script lang="ts">
  // "My requests" (EF-UX-010): one state per request, in the family's words, and the link that
  // opens Kavita when it is there. A request that was refused, not found or withdrawn says so, and
  // gives the reason the admin wrote.
  import type { Snippet } from 'svelte';
  import type { RequestOut } from '#lib/api/types.gen.ts';
  import OpenInKavita from '#lib/components/OpenInKavita.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import Badge, { type BadgeTone } from '#lib/ui/atoms/Badge.svelte';

  let { request, actions }: { request: RequestOut; actions?: Snippet } = $props();

  const WORDS: Record<string, MessageKey> = {
    preparing: 'activity.state.preparing',
    available: 'activity.state.available',
    waiting: 'activity.state.waiting',
    rejected: 'request.state.rejected',
    not_found: 'request.state.not_found',
    cancelled: 'request.state.cancelled'
  };
  const TONES: Record<string, BadgeTone> = {
    available: 'success',
    preparing: 'accent',
    waiting: 'neutral',
    rejected: 'danger',
    not_found: 'danger',
    cancelled: 'neutral'
  };
  const state = $derived(request.family_state ?? request.state);
  const word = $derived(WORDS[state]);
</script>

<article class="request-item">
  <div class="request-item__text">
    {#if request.title}
      <p class="request-item__title">{request.title}</p>
    {/if}
    {#if request.reject_reason}
      <p class="request-item__reason">{t('request.reason', { reason: request.reject_reason })}</p>
    {/if}
  </div>
  {#if word}
    <Badge tone={TONES[state] ?? 'neutral'}>{t(word)}</Badge>
  {/if}
  {#if request.family_state === 'available'}
    <OpenInKavita url={request.open_url} />
  {/if}
  {#if actions}{@render actions()}{/if}
</article>

<style>
  .request-item {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-4);
    padding-block: var(--space-3);
  }
  .request-item__text {
    flex: 1 1 14rem;
    min-width: 0;
  }
  .request-item__title {
    margin: 0;
    font: 600 var(--step-1) / var(--leading-tight) var(--font-text);
    overflow-wrap: anywhere;
  }
  .request-item__reason {
    margin: var(--space-1) 0 0;
    color: var(--color-muted);
    font-size: var(--step--1);
  }
</style>
