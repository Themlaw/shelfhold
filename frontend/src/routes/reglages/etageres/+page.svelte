<script lang="ts">
  import { onMount } from 'svelte';
  import { client } from '#lib/api/client.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import PlanPreview from '#lib/components/PlanPreview.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { isForbidden, needsReauth, problemOf } from '#lib/triage.ts';

  type Template = {
    chapter: string;
    volume: string;
    special: string;
    special_dir: string;
    segment: string;
  };
  type Destination = {
    id: string;
    shelf_id: string;
    shelf_name: string;
    target_id: string;
    root_subpath: string;
    transport: string;
    adopted: boolean;
    flagged_names: number;
    template: Template;
  };
  type PreviewEntry = {
    key: string;
    rel_path: string;
    template_ref: string;
    status: string;
    flags: string[];
  };
  type NamingPreview = {
    verified: boolean;
    entries: PreviewEntry[];
    collisions: { kind: string; names: string[] }[];
  };
  type RenamePlan = {
    plan_id: string | null;
    base_version: string;
    steps: { kind: string; src: string; dst: string; export_item_id: string }[];
    batches: number;
    blocked: { code: string; src: string }[];
    warnings: { code: string; readers: number; exact: boolean; series: string[] }[];
  };

  let shelves = $state<{ id: string; name: string; max_age_rating: number | null }[]>([]);
  let destinations = $state<Destination[]>([]);
  let adminOnly = $state(false);
  let failed = $state(false);
  let message = $state('');
  let failedKind = $state(false);

  // per-destination state
  type DestState = {
    template: Template;
    original: Template;
    preview: NamingPreview | null;
    previewOpen: boolean;
    previewLoading: boolean;
    plan: RenamePlan | null;
    planEtag: string | null;
    applying: boolean;
  };
  let states = $state<Record<string, DestState>>({});

  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  function statusLabel(status: string, verified: boolean): string {
    if (!verified) return t('shelves.naming.unverified');
    if (status === 'confirmed') return t('shelves.naming.confirmed');
    if (status === 'flagged') return t('shelves.naming.contested');
    return t('shelves.naming.unverified');
  }

  async function load(): Promise<void> {
    try {
      const shelvesRes = await client.get({ url: '/api/v1/shelves', throwOnError: true });
      shelves = (shelvesRes.data as { items: typeof shelves }).items ?? [];
    } catch (error) {
      if (isForbidden(error)) {
        adminOnly = true;
        message = t('screens.admin_only');
        return;
      }
      failed = true;
      message = t('shelves.error');
      return;
    }
    try {
      const destRes = await client.get({ url: '/api/v1/destinations', throwOnError: true });
      destinations = (destRes.data as { items: Destination[] }).items ?? [];
    } catch (error) {
      if (isForbidden(error)) {
        adminOnly = true;
        message = t('screens.admin_only');
        return;
      }
      failed = true;
      message = t('shelves.error');
      return;
    }
    const next: Record<string, DestState> = {};
    for (const d of destinations) {
      next[d.id] = {
        template: { ...d.template },
        original: { ...d.template },
        preview: null,
        previewOpen: d.flagged_names > 0,
        previewLoading: false,
        plan: null,
        planEtag: null,
        applying: false
      };
    }
    states = next;
    // initial previews for contested names
    for (const d of destinations) {
      if (d.flagged_names > 0) {
        void fetchPreview(d, states[d.id].template);
      }
    }
  }

  async function fetchPreview(dest: Destination, template: Template): Promise<void> {
    const st = states[dest.id];
    if (!st) return;
    st.previewLoading = true;
    try {
      const { data, error } = await client.post({
        url: '/api/v1/plans/naming-preview',
        headers: headers(),
        body: { shelf_destination_id: dest.id, template },
        throwOnError: false
      });
      if (error) return;
      st.preview = data as NamingPreview;
      st.previewOpen = true;
    } finally {
      st.previewLoading = false;
    }
  }

  function isChanged(dest: Destination): boolean {
    const st = states[dest.id];
    if (!st) return false;
    return JSON.stringify(st.template) !== JSON.stringify(st.original);
  }

  function onFieldInput(dest: Destination, field: keyof Template, value: string): void {
    const st = states[dest.id];
    if (!st) return;
    st.template = { ...st.template, [field]: value };
    if (isChanged(dest)) {
      void fetchPreview(dest, st.template);
    }
  }

  async function rename(dest: Destination): Promise<void> {
    const st = states[dest.id];
    if (!st) return;
    failedKind = false;
    message = '';
    try {
      const res = await client.post({
        url: '/api/v1/plans/renames',
        headers: headers(),
        body: { shelf_destination_id: dest.id, template: st.template },
        throwOnError: true
      });
      const data = res.data as RenamePlan;
      const etag = (res.response as Response | undefined)?.headers?.get('etag') ?? null;
      st.plan = data;
      st.planEtag = etag;
    } catch (error) {
      if (needsReauth(error)) {
        retry = () => rename(dest);
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        failed = true;
        message = t('shelves.refused');
      }
    }
  }

  async function applyPlan(dest: Destination, confirmProgressLoss: boolean): Promise<void> {
    const st = states[dest.id];
    if (!st?.plan) return;
    st.applying = true;
    try {
      await client.post({
        url: '/api/v1/plans/{plan_id}/apply',
        path: { plan_id: st.plan.plan_id },
        headers: { ...headers(), 'If-Match': st.planEtag ?? '' },
        body: { confirm_progress_loss: confirmProgressLoss },
        throwOnError: true
      });
      message = t('shelves.apply_started');
      failed = false;
    } catch (error) {
      if (needsReauth(error)) {
        retry = () => applyPlan(dest, confirmProgressLoss);
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else if (problemOf(error).code === 'plan.stale' || problemOf(error).type?.endsWith('plan.stale')) {
        failed = true;
        message = t('shelves.plan_stale');
      } else {
        failed = true;
        message = t('shelves.refused');
      }
    } finally {
      st.applying = false;
    }
  }

  onMount(load);
</script>

<svelte:head>
  <title>{t('shelves.title')}</title>
</svelte:head>

<main>
  <h1>{t('shelves.title')}</h1>
  {#if adminOnly}
    <p role="alert">{message}</p>
  {:else if failed && !adminOnly && message && destinations.length === 0}
    <p role="alert">{message}</p>
  {:else}
    {#if destinations.length === 0}
      <p>{t('shelves.empty')}</p>
    {/if}
    <ul>
      {#each destinations as dest (dest.id)}
        {@const st = states[dest.id]}
        {#if st}
          <li>
            <span>{dest.shelf_name}</span>
            <span>{dest.root_subpath}</span>
            {#each ['chapter', 'volume', 'special', 'special_dir', 'segment'] as field (field)}
              <label>
                {t(`shelves.field.${field}`)}
                <input
                  type="text"
                  value={st.template[field as keyof Template]}
                  oninput={(e) => onFieldInput(dest, field as keyof Template, (e.currentTarget as HTMLInputElement).value)}
                />
              </label>
            {/each}
            <details open={st.previewOpen}>
              <summary>{t('shelves.naming.title')}</summary>
              {#if st.preview}
                <ul>
                  {#each st.preview.entries as entry (entry.key)}
                    <li>
                      {entry.rel_path}
                      {statusLabel(entry.status, st.preview.verified)}
                    </li>
                  {/each}
                </ul>
                {#each st.preview.collisions as col}
                  <p>{col.names.join(', ')}</p>
                {/each}
              {/if}
            </details>
            <button type="button" disabled={!isChanged(dest)} onclick={() => rename(dest)}>
              {t('shelves.rename')}
            </button>
            {#if st.plan}
              <PlanPreview
                plan={st.plan}
                onapply={(r) => applyPlan(dest, r.confirmProgressLoss)}
                oncancel={() => { st.plan = null; }}
              />
            {/if}
          </li>
        {/if}
      {/each}
    </ul>
  {/if}
  {#if message && !adminOnly}
    <p role={failed ? 'alert' : 'status'}>{message}</p>
  {/if}
  {#if reauthMethods}
    <ReauthPrompt
      methods={reauthMethods}
      ondone={async () => {
        reauthMethods = null;
        const r = retry;
        retry = null;
        await r?.();
      }}
      oncancel={() => { reauthMethods = null; retry = null; }}
    />
  {/if}
</main>

<style>
  main { max-width: 60rem; margin: 0 auto; padding: 2rem 1rem; }
  ul { list-style: none; padding: 0; }
  li { margin-bottom: 2rem; }
  details { margin: 0.5rem 0; }
</style>