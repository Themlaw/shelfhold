<script lang="ts">
  // The shelves screen (EF-EXP-002, EF-EXP-005, EF-EXP-030, EF-EXP-037): the naming template of
  // each destination, the naming preview (folded unless the template was changed or the server
  // says names are contested), the rename plan with its warning, and its application.
  import { onDestroy, onMount } from 'svelte';
  import { client } from '#lib/api/client.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import PlanPreview from '#lib/components/PlanPreview.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { isForbidden, needsReauth, problemOf } from '#lib/triage.ts';

  const FIELDS = ['chapter', 'volume', 'special', 'special_dir', 'segment'] as const;
  type Template = Record<(typeof FIELDS)[number], string>;
  type Destination = {
    id: string;
    shelf_id: string;
    shelf_name: string;
    root_subpath: string;
    flagged_names: number | null; // null: the count failed for this destination
    template: Template;
  };
  type PreviewEntry = { key: string; rel_path: string; status: string };
  type NamingPreview = {
    verified: boolean;
    entries: PreviewEntry[];
    collisions: { kind: string; names: string[] }[];
  };
  type RenamePlan = {
    plan_id: string | null;
    base_version: string;
    steps: { kind: string; src: string; dst: string }[];
    batches: number;
    blocked: { code: string; src: string }[];
    warnings: { code: string; readers: number; exact: boolean; series: string[] }[];
  };
  type Shelf = { id: string; name: string };
  type DestState = {
    template: Template;
    preview: NamingPreview | null;
    previewOpen: boolean;
    asked: number;
    plan: RenamePlan | null;
    planEtag: string | null;
  };

  const DEBOUNCE_MS = 300;

  let shelves = $state<Shelf[]>([]);
  let destinations = $state<Destination[]>([]);
  let adminOnly = $state(false);
  let notice = $state<{ text: string; kind: 'status' | 'alert' } | null>(null);
  let states = $state<Record<string, DestState>>({});
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;
  const timers: Record<string, ReturnType<typeof setTimeout>> = {};

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const say = (text: string, kind: 'status' | 'alert') => (notice = { text, kind });
  const sameTemplate = (a: Template, b: Template) => FIELDS.every((field) => a[field] === b[field]);
  const unplaced = $derived(shelves.filter((s) => !destinations.some((d) => d.shelf_id === s.id)));

  function statusLabel(status: string, verified: boolean): string {
    if (!verified) return t('shelves.naming.unverified');
    if (status === 'confirmed') return t('shelves.naming.confirmed');
    if (status === 'flagged') return t('shelves.naming.contested');
    return t('shelves.naming.unverified');
  }

  async function load(): Promise<void> {
    const shelvesAnswer = await client.get({ url: '/api/v1/shelves', throwOnError: false });
    const destinationsAnswer = await client.get({ url: '/api/v1/destinations', throwOnError: false });
    const error = shelvesAnswer.error ?? destinationsAnswer.error;
    if (error) {
      adminOnly = isForbidden(error);
      say(adminOnly ? t('screens.admin_only') : t('shelves.error'), 'alert');
      return;
    }
    shelves = (shelvesAnswer.data as { items: Shelf[] }).items;
    destinations = (destinationsAnswer.data as { items: Destination[] }).items;
    const next: Record<string, DestState> = {};
    for (const d of destinations) {
      next[d.id] = {
        template: { ...d.template },
        preview: null,
        previewOpen: (d.flagged_names ?? 0) > 0,
        asked: 0,
        plan: null,
        planEtag: null
      };
    }
    states = next;
    for (const d of destinations) if ((d.flagged_names ?? 0) > 0) void fetchPreview(d);
  }

  /** The names of the template as it stands in the fields; a late answer never replaces a later one. */
  async function fetchPreview(dest: Destination): Promise<void> {
    const st = states[dest.id];
    if (!st) return;
    const ticket = ++st.asked;
    const { data, error } = await client.post({
      url: '/api/v1/plans/naming-preview',
      headers: headers(),
      body: { shelf_destination_id: dest.id, template: $state.snapshot(st.template) },
      throwOnError: false
    });
    if (ticket !== st.asked) return;
    if (error) {
      say(t('shelves.naming.error'), 'alert');
      return;
    }
    st.preview = data as NamingPreview;
    st.previewOpen = true;
  }

  function onField(dest: Destination, field: keyof Template, value: string): void {
    const st = states[dest.id];
    if (!st) return;
    st.template[field] = value;
    st.plan = null; // a plan was computed for another template
    clearTimeout(timers[dest.id]);
    if (sameTemplate(st.template, dest.template)) return;
    timers[dest.id] = setTimeout(() => void fetchPreview(dest), DEBOUNCE_MS);
  }

  function askAgain(error: unknown, replay: () => Promise<void>): boolean {
    if (!needsReauth(error)) return false;
    retry = replay;
    reauthMethods = problemOf(error).reauth_methods ?? ['password'];
    return true;
  }

  async function rename(dest: Destination): Promise<void> {
    const st = states[dest.id];
    if (!st) return;
    notice = null;
    const { data, error, response } = await client.post({
      url: '/api/v1/plans/renames',
      headers: headers(),
      body: { shelf_destination_id: dest.id, template: $state.snapshot(st.template) },
      throwOnError: false
    });
    if (error) {
      if (!askAgain(error, () => rename(dest))) say(t('shelves.refused'), 'alert');
      return;
    }
    const plan = data as RenamePlan;
    st.plan = plan;
    st.planEtag = response?.headers.get('etag') ?? `"${plan.base_version}"`; // the version the plan was computed on
  }

  async function applyPlan(dest: Destination, confirmProgressLoss: boolean): Promise<void> {
    const st = states[dest.id];
    const planId = st?.plan?.plan_id;
    if (!st || !planId) return;
    const { error } = await client.post({
      url: '/api/v1/plans/{plan_id}/apply',
      path: { plan_id: planId },
      headers: { ...headers(), 'If-Match': st.planEtag ?? '' },
      body: { confirm_progress_loss: confirmProgressLoss },
      throwOnError: false
    });
    if (error) {
      if (askAgain(error, () => applyPlan(dest, confirmProgressLoss))) return;
      const stale = problemOf(error).type?.endsWith('plan.stale') || problemOf(error).code === 'plan.stale';
      if (stale) st.plan = null; // recomputed on « Renommer »
      say(stale ? t('shelves.plan_stale') : t('shelves.refused'), 'alert');
      return;
    }
    st.plan = null;
    say(t('shelves.apply_started'), 'status');
  }

  onMount(load);
  onDestroy(() => Object.values(timers).forEach((timer) => clearTimeout(timer)));
</script>

<svelte:head>
  <title>{t('shelves.title')}</title>
</svelte:head>

<main>
  <h1>{t('shelves.title')}</h1>
  {#if adminOnly}
    <p role="alert">{notice?.text}</p>
  {:else}
    {#if destinations.length === 0}
      <p>{t('shelves.empty')}</p>
    {/if}
    <ul>
      {#each destinations as dest (dest.id)}
        {@const st = states[dest.id]}
        {#if st}
          <li>
            <fieldset>
              <legend>{dest.shelf_name} <span>{dest.root_subpath}</span></legend>
              {#each FIELDS as field (field)}
                <label>
                  {t(`shelves.field.${field}`)}
                  <input
                    type="text"
                    value={st.template[field]}
                    oninput={(event) => onField(dest, field, event.currentTarget.value)}
                  />
                </label>
              {/each}
            </fieldset>
            {#if dest.flagged_names === null}
              <p>{t('shelves.naming.unknown')}</p>
            {/if}
            <details open={st.previewOpen} ontoggle={(event) => (st.previewOpen = event.currentTarget.open)}>
              <summary>{t('shelves.naming.title')}</summary>
              {#if st.preview}
                {@const verified = st.preview.verified}
                <ul>
                  {#each st.preview.entries as entry (entry.key)}
                    <li><span>{entry.rel_path}</span> <span>{statusLabel(entry.status, verified)}</span></li>
                  {/each}
                </ul>
                {#each st.preview.collisions as collision (collision.names.join('\u0000'))}
                  <p>{collision.names.join(', ')}</p>
                {/each}
              {/if}
            </details>
            <button type="button" disabled={sameTemplate(st.template, dest.template)} onclick={() => rename(dest)}>
              {t('shelves.rename')}
            </button>
            {#if st.plan}
              <PlanPreview
                plan={st.plan}
                onapply={(result) => applyPlan(dest, result.confirmProgressLoss)}
                oncancel={() => (st.plan = null)}
              />
            {/if}
          </li>
        {/if}
      {/each}
    </ul>
    {#each unplaced as shelf (shelf.id)}
      <p>{t('shelves.no_destination', { name: shelf.name })}</p>
    {/each}
    {#if notice}
      <p role={notice.kind}>{notice.text}</p>
    {/if}
  {/if}
  {#if reauthMethods}
    <ReauthPrompt
      methods={reauthMethods}
      ondone={async () => {
        reauthMethods = null;
        const replay = retry;
        retry = null;
        await replay?.();
      }}
      oncancel={() => {
        reauthMethods = null;
        retry = null;
      }}
    />
  {/if}
</main>

<style>
  main { max-width: 60rem; margin: 0 auto; padding: 2rem 1rem; }
  ul { list-style: none; padding: 0; }
  li { margin-bottom: 2rem; }
  details { margin: 0.5rem 0; }
  label { display: block; margin: 0.25rem 0; }
</style>
