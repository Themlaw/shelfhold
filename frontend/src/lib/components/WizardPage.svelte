<script lang="ts">
  // The page of a step of the first-run wizard: the book-like head (the running head, the one h1
  // of the screen, the rule of five steps) and the step's own title, then the step. The wizard is
  // met before anyone is signed in, so nothing here assumes a session.
  import type { Snippet } from 'svelte';
  import { t } from '#lib/i18n/index.ts';
  import WizardSteps from '#lib/components/WizardSteps.svelte';
  import type { WizardStep } from '#lib/wizard.ts';

  let {
    current,
    title,
    lede,
    children
  }: { current: WizardStep['id']; title: string; lede?: string; children: Snippet } = $props();
</script>

<main class="page page--form wizard">
  <div class="running-head"><span>{t('admin.wizard.title')}</span></div>
  <h1 class="page-title">{t('admin.wizard.title')}</h1>
  <WizardSteps {current} />
  <h2 class="wizard__step">{title}</h2>
  {#if lede}<p class="lede wizard__lede">{lede}</p>{/if}
  <div class="wizard__body">{@render children()}</div>
</main>

<style>
  .page-title {
    margin-bottom: var(--space-4);
  }
  .wizard__step {
    margin: 0 0 var(--space-3);
    font-size: var(--step-2);
  }
  .wizard__lede {
    margin: 0 0 var(--space-5);
    font-size: var(--step-0);
  }
  .wizard__body {
    display: grid;
    gap: var(--space-5);
  }
  /* the steps' own parts (shelves, sources, jurisdiction, finish) are drawn here so the screens carry no style of their own */
  .wizard__body :global(.intro) {
    margin: 0;
    max-width: var(--measure);
  }
  .wizard__body :global(.shelves) {
    display: grid;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .wizard__body :global(.advanced) {
    border: var(--rule-hair) solid var(--color-rule);
    border-radius: var(--radius-sm);
    padding: 0 var(--space-4);
  }
  .wizard__body :global(.advanced summary) {
    display: flex;
    align-items: center;
  }
  .wizard__body :global(.advanced[open]) {
    padding-bottom: var(--space-3);
  }
  .wizard__body :global(.sources) {
    margin: 0;
    padding: var(--space-4);
    border: var(--rule-hair) solid var(--color-rule);
    border-radius: var(--radius-sm);
  }
  .wizard__body :global(.sources .hint) {
    margin-top: 0;
  }
  .wizard__body :global(.step) {
    display: grid;
    gap: var(--space-3);
  }
  .wizard__body :global(.step h3) {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
    margin: 0;
    font-size: var(--step-1);
  }
  .wizard__body :global(.step p) {
    margin: 0;
    max-width: var(--measure);
  }
  .wizard__body :global(.finish) {
    padding-top: var(--space-4);
    border-top: var(--rule-hair) solid var(--color-ink);
  }
</style>
