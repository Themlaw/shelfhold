<script lang="ts">
  // The place of the notices (toast.svelte.ts). The live region is always in the page, so a
  // screen reader hears the notice when it arrives; a danger is announced at once (alert). On a
  // wide screen the notice is printed in the header, between the search and the account; under
  // 60 rem it docks on the tab bar, full width, and the layout keeps its height free under the
  // colophon (--notice-h): the end of the page is never hidden.
  import { t } from '#lib/i18n/index.ts';
  import Icon from '../atoms/Icon.svelte';
  import type { IconName } from '../atoms/icons.ts';
  import { dismiss, notices, type NoticeTone } from '../toast.svelte.ts';

  const ICON: Record<NoticeTone, IconName> = { success: 'check', info: 'info', warning: 'alert', danger: 'alert' };

  const notice = $derived(notices.current);
</script>

<div class="notice-region" role="status" aria-live="polite" aria-atomic="true">
  {#if notice && notice.tone !== 'danger'}
    <div class="notice notice--{notice.tone}">
      <Icon name={ICON[notice.tone]} />
      <div class="notice__text">
        <p class="notice__title">{notice.title}</p>
        {#if notice.body}<p class="notice__body">{notice.body}</p>{/if}
      </div>
      {#if notice.action}
        <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
        <a class="notice__action" href={notice.action.href}>{notice.action.label}</a>
      {/if}
      <button class="notice__close" type="button" aria-label={t('ui.notice.close')} title={t('ui.notice.close')} onclick={() => dismiss(notice.id)}>
        <Icon name="x" />
      </button>
    </div>
  {/if}
</div>
<div class="notice-region" role="alert" aria-atomic="true">
  {#if notice && notice.tone === 'danger'}
    <div class="notice notice--danger">
      <Icon name="alert" />
      <div class="notice__text">
        <p class="notice__title">{notice.title}</p>
        {#if notice.body}<p class="notice__body">{notice.body}</p>{/if}
      </div>
      <button class="notice__close" type="button" aria-label={t('ui.notice.close')} title={t('ui.notice.close')} onclick={() => dismiss(notice.id)}>
        <Icon name="x" />
      </button>
    </div>
  {/if}
</div>

<style>
  .notice-region {
    display: contents;
  }
  .notice {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    flex: 0 1 30rem;
    min-width: 0;
    padding: var(--space-1) var(--space-1) var(--space-1) var(--space-3);
    background: var(--color-surface-float);
    color: var(--color-ink);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-left: var(--rule-thick) solid var(--color-ok);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-2);
  }
  .notice > :global(.icon) {
    color: var(--color-ok);
  }
  .notice--info {
    border-left-color: var(--color-accent);
  }
  .notice--info > :global(.icon) {
    color: var(--color-accent-text);
  }
  .notice--warning {
    border-left-color: var(--color-warn);
  }
  .notice--warning > :global(.icon) {
    color: var(--color-warn);
  }
  .notice--danger {
    border-left-color: var(--color-err);
  }
  .notice--danger > :global(.icon) {
    color: var(--color-err);
  }
  .notice__text {
    flex: 1;
    min-width: 0;
  }
  .notice__title {
    margin: 0;
    font-weight: 700;
    line-height: 1.25;
  }
  .notice__body {
    margin: 0;
    font-size: var(--step--1);
    line-height: 1.3;
    color: var(--color-muted);
  }
  .notice__action {
    flex: none;
    display: inline-flex;
    align-items: center;
    min-height: var(--target);
    font-weight: 500;
  }
  .notice__close {
    flex: none;
    display: inline-grid;
    place-items: center;
    width: var(--target);
    height: var(--target);
    padding: 0;
    border: 0;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--color-muted);
    cursor: pointer;
  }
  .notice__close:hover {
    background: var(--color-surface-2);
    color: var(--color-ink);
  }
  @media (max-width: 60rem) {
    .notice {
      position: fixed;
      left: 0;
      right: 0;
      bottom: var(--tabbar-h);
      z-index: var(--z-toast);
      min-height: var(--notice-h);
      border: 0;
      border-top: var(--rule-hair) solid var(--color-ink);
      border-radius: 0;
      box-shadow: inset var(--rule-thick) 0 0 var(--color-ok);
      padding: var(--space-2) var(--space-2) var(--space-2) var(--space-4);
    }
    .notice--info {
      box-shadow: inset var(--rule-thick) 0 0 var(--color-accent);
    }
    .notice--warning {
      box-shadow: inset var(--rule-thick) 0 0 var(--color-warn);
    }
    .notice--danger {
      box-shadow: inset var(--rule-thick) 0 0 var(--color-err);
    }
  }
</style>
