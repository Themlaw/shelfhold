<script lang="ts">
  // The destinations of one shelf (EF-EXP-002, EF-EXP-047): a shelf takes N of them, each with its
  // server, its transport and what it keeps apart, and a new one is linked by choosing a server.
  // The server is the gate: a shelf below 18+ is refused a server that does not keep accounts
  // apart, and the refusal is shown as it was written (its stable code, its title, its cause and
  // its action) — this component decides nothing, it only does not hide the reason.
  import { onMount } from 'svelte';
  import { client } from '#lib/api/client.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { hasMessage, t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';

  export type Target = { id: string; name: string; kind: string; partitioning: string };
  type Binding = {
    id: string;
    target_id: string;
    transport: string;
    role: 'primary' | 'secondary' | null;
    partitioning: string;
  };

  let {
    shelf,
    targets
  }: { shelf: { id: string; name: string; max_age_rating: number | null }; targets: Target[] } =
    $props();

  const TRANSPORTS = ['tree', 'dropbox', 'pull'] as const;
  const family = $derived(shelf.max_age_rating !== null && shelf.max_age_rating < 18);

  let bindings = $state<Binding[]>([]);
  let loadFailed = $state(false);
  let targetId = $state('');
  let transport = $state<(typeof TRANSPORTS)[number]>('tree');
  let folder = $state('');
  let busy = $state(false);
  let linked = $state(false);
  let refusal = $state<ProblemView | null>(null);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const nameOf = (id: string): string => targets.find((target) => target.id === id)?.name ?? id;
  const label = (prefix: string, value: string): string => {
    const key = `${prefix}.${value}`;
    return hasMessage(key) ? t(key) : value;
  };

  function askAgain(error: unknown, replay: () => Promise<void>): boolean {
    if (!needsReauth(error)) return false;
    retry = replay;
    reauthMethods = problemOf(error).reauth_methods ?? ['password'];
    return true;
  }

  async function load(): Promise<void> {
    const { data, error } = await client.get({
      url: '/api/v1/shelves/{shelf_id}/destinations',
      path: { shelf_id: shelf.id },
      throwOnError: false
    });
    if (error) {
      if (!askAgain(error, load)) loadFailed = true;
      return;
    }
    loadFailed = false;
    bindings = (data as { destinations: Binding[] }).destinations;
  }

  async function link(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (busy || targetId === '') return;
    busy = true;
    linked = false;
    refusal = null;
    try {
      const { error } = await client.post({
        url: '/api/v1/shelves/{shelf_id}/destinations',
        path: { shelf_id: shelf.id },
        headers: headers(),
        body: { target_id: targetId, transport, root_subpath: folder.trim() },
        throwOnError: false
      });
      if (error) {
        if (askAgain(error, () => link(event))) return;
        refusal = problemView(error);
        return;
      }
      linked = true;
      folder = '';
      await load();
    } finally {
      busy = false;
    }
  }

  onMount(load);
</script>

<section aria-labelledby="bindings-{shelf.id}">
  <h2 id="bindings-{shelf.id}">{t('bindings.title', { name: shelf.name })}</h2>
  {#if family}
    <p><strong>{t('bindings.family')}</strong> — {t('bindings.family_hint')}</p>
  {/if}
  {#if loadFailed}
    <p role="alert">{t('bindings.loading_error')}</p>
  {:else if bindings.length === 0}
    <p>{t('bindings.empty')}</p>
  {:else}
    <ul>
      {#each bindings as binding (binding.id)}
        <li>
          <strong>{nameOf(binding.target_id)}</strong>
          <span>{label('bindings.transport', binding.transport)}</span>
          <span>{label('bindings.partitioning', binding.partitioning)}</span>
          {#if binding.role}<span>{label('bindings.role', binding.role)}</span>{/if}
        </li>
      {/each}
    </ul>
  {/if}

  <form onsubmit={link}>
    <h3>{t('bindings.add')}</h3>
    <label>
      {t('bindings.target')}
      <select bind:value={targetId}>
        <option value="" disabled>{t('bindings.target_none')}</option>
        {#each targets as target (target.id)}
          <option value={target.id}>{target.name}</option>
        {/each}
      </select>
    </label>
    <label>
      {t('bindings.transport')}
      <select bind:value={transport}>
        {#each TRANSPORTS as kind (kind)}
          <option value={kind}>{t(`bindings.transport.${kind}`)}</option>
        {/each}
      </select>
    </label>
    <label>
      {t('bindings.folder')}
      <input type="text" bind:value={folder} autocomplete="off" spellcheck="false" />
    </label>
    <button type="submit" disabled={busy || targetId === ''}>{t('bindings.submit')}</button>
  </form>

  {#if linked}
    <p role="status">{t('bindings.linked')}</p>
  {/if}
  {#if refusal}
    <ProblemCard problem={refusal} level={3} />
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
</section>

<style>
  section { margin: 1.5rem 0; }
  ul { list-style: none; padding: 0; }
  li { display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; margin-bottom: 0.5rem; }
  form { display: grid; gap: 0.5rem; max-width: 30rem; }
  label { display: grid; gap: 0.25rem; }
</style>
