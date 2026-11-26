<script lang="ts">
  // « Passer à l'équipe préférée » (EF-BIB-005, M4 L35): a preview, then the application, then the
  // report. Choosing a team only orders the teams; this is the second step, which replaces files
  // already served, so the server asks for a fresh admin session (a refusal is shown as a problem).
  // The preview says what would be replaced, what is refused (with the word of why) and what waits
  // for the preferred team; the application is a job on the server, read until it ends. The server
  // is the only source of what is shown: no path and no name of a file ever reaches this screen.
  import { teamSwitchApply, teamSwitchPreview, teamSwitchReport } from '#lib/api/index.ts';
  import type { TeamSwitchOut } from '#lib/api/types.gen.ts';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import { hasMessage, t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';

  let { editionId }: { editionId: string } = $props();

  const ENDED = ['done', 'stale', 'failed'];
  const POLL_MS = 1000;
  const POLL_TRIES = 120;

  let view = $state<TeamSwitchOut | null>(null);
  let running = $state(false);
  let problem = $state<ProblemView | null>(null);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const ended = $derived(view !== null && ENDED.includes(view.state));

  /** The refusals, counted by their word: « Original irremplaçable (2) ». */
  function reasons(blocked: Record<string, string[]>): { word: string; count: number }[] {
    const counts: Record<string, number> = {};
    for (const words of Object.values(blocked)) {
      for (const word of words) counts[word] = (counts[word] ?? 0) + 1;
    }
    return Object.entries(counts).map(([word, count]) => ({ word, count }));
  }

  function reasonLabel(word: string): string {
    const key = `switch.reason.${word}`;
    return t(hasMessage(key) ? (key as MessageKey) : 'switch.reason.other');
  }

  async function preview(): Promise<void> {
    problem = null;
    view = null;
    const { data, error } = await teamSwitchPreview({
      headers: headers(),
      path: { edition_id: editionId }
    });
    if (error !== undefined || !data) {
      problem = problemView(error);
      return;
    }
    view = data;
  }

  async function read(switchId: string): Promise<TeamSwitchOut | null> {
    const { data, error } = await teamSwitchReport({
      headers: headers(),
      path: { edition_id: editionId, switch_id: switchId }
    });
    if (error !== undefined || !data) {
      problem = problemView(error);
      return null;
    }
    return data;
  }

  async function apply(): Promise<void> {
    if (!view) return;
    const switchId = view.id;
    problem = null;
    running = true;
    try {
      const { error } = await teamSwitchApply({
        headers: headers(),
        path: { edition_id: editionId, switch_id: switchId }
      });
      if (error !== undefined) {
        problem = problemView(error);
        return;
      }
      for (let tries = 0; tries < POLL_TRIES; tries += 1) {
        const latest = await read(switchId);
        if (latest === null) return;
        view = latest;
        if (ENDED.includes(latest.state)) return;
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      }
    } finally {
      running = false;
    }
  }

  const count = (list: unknown[]) => ({ count: list.length });
</script>

<section class="section" aria-labelledby="switch-title">
  <div class="section-head"><h2 id="switch-title">{t('switch.title')}</h2></div>
  <p class="hint">{t('switch.intro')}</p>

  <div class="stack">
    {#if problem}
      <ProblemAlert {problem} />
    {/if}

    {#if view?.state === 'stale'}
      <Alert tone="warning" live="polite" title={t('switch.stale')} />
    {:else if ended && view}
      {#if view.state === 'failed'}
        <Alert tone="danger" live="polite" title={t('switch.failed')} />
      {/if}
      <h3>{t('switch.result')}</h3>
      <ul class="entries counts">
        <li>{t('switch.done_switched', count(view.switched))}</li>
        <li>{t('switch.done_blocked', count(Object.keys(view.blocked)))}</li>
        <li>{t('switch.done_pending', count(view.pending))}</li>
      </ul>
      {#if Object.keys(view.blocked).length > 0}
        <ul class="entries counts">
          {#each reasons(view.blocked) as reason (reason.word)}
            <li>{reasonLabel(reason.word)} ({reason.count})</li>
          {/each}
        </ul>
      {/if}
    {:else if view}
      {#if view.to_switch.length === 0 && Object.keys(view.blocked).length === 0 && view.pending.length === 0}
        <Alert tone="info" live="polite" title={t('switch.nothing')} />
      {:else}
        <ul class="entries counts">
          <li>{t('switch.to_switch', count(view.to_switch))}</li>
          <li>{t('switch.blocked', count(Object.keys(view.blocked)))}</li>
          <li>{t('switch.pending', count(view.pending))}</li>
        </ul>
        {#if Object.keys(view.blocked).length > 0}
          <ul class="entries counts">
            {#each reasons(view.blocked) as reason (reason.word)}
              <li>{reasonLabel(reason.word)} ({reason.count})</li>
            {/each}
          </ul>
        {/if}
        {#if view.to_switch.length > 0}
          <div class="btn-row">
            <Button variant="primary" loading={running} onclick={apply}>
              {running ? t('switch.running') : t('switch.apply')}
            </Button>
          </div>
        {/if}
      {/if}
    {/if}

    {#if !running}
      <div class="btn-row">
        <Button variant="secondary" onclick={preview}>{t('switch.preview')}</Button>
      </div>
    {/if}
  </div>
</section>

<style>
  .counts > li {
    padding: var(--space-2) 0;
    overflow-wrap: anywhere;
  }
</style>
