<script lang="ts">
  // The destinations of one shelf (EF-EXP-002, EF-EXP-047): a shelf takes N of them, each with its
  // server, its transport and what it keeps apart, and a new one is linked by choosing a server.
  // The server is the gate: a shelf below 18+ is refused a server that does not keep accounts
  // apart, and the refusal is shown as it was written (its stable code, its title, its cause and
  // its action) — this component decides nothing, it only does not hide the reason.
  import { onMount } from 'svelte';
  import { client } from '#lib/api/client.gen.ts';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { hasMessage, t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';

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

<section class="bindings" aria-labelledby="bindings-{shelf.id}">
  <h2 id="bindings-{shelf.id}">{t('bindings.title', { name: shelf.name })}</h2>
  {#if family}
    <p><Badge tone="accent" icon="users">{t('bindings.family')}</Badge> <span class="muted">{t('bindings.family_hint')}</span></p>
  {/if}
  {#if loadFailed}
    <Alert tone="danger" live="assertive" title={t('bindings.loading_error')} />
  {:else if bindings.length === 0}
    <p class="muted">{t('bindings.empty')}</p>
  {:else}
    <ul class="entries">
      {#each bindings as binding (binding.id)}
        <li class="binding">
          <strong>{nameOf(binding.target_id)}</strong>
          <span class="muted">{label('bindings.transport', binding.transport)}</span>
          <span class="muted">{label('bindings.partitioning', binding.partitioning)}</span>
          {#if binding.role}<span class="muted">{label('bindings.role', binding.role)}</span>{/if}
        </li>
      {/each}
    </ul>
  {/if}

  <form class="add" onsubmit={link}>
    <h3>{t('bindings.add')}</h3>
    <Field label={t('bindings.target')}>
      {#snippet children(control)}
        <Select
          {...control}
          bind:value={targetId}
          options={[
            { value: '', label: t('bindings.target_none'), disabled: true },
            ...targets.map((target) => ({ value: target.id, label: target.name }))
          ]}
        />
      {/snippet}
    </Field>
    <Field label={t('bindings.transport')}>
      {#snippet children(control)}
        <Select
          {...control}
          bind:value={transport}
          options={TRANSPORTS.map((kind) => ({ value: kind, label: t(`bindings.transport.${kind}`) }))}
        />
      {/snippet}
    </Field>
    <Field label={t('bindings.folder')}>
      {#snippet children(control)}
        <Input {...control} type="text" mono bind:value={folder} autocomplete="off" spellcheck={false} />
      {/snippet}
    </Field>
    <!-- not the lone last child of the form: one form per shelf, so one sticky action per shelf
         would stack at the bottom of a phone and cover the fields -->
    <div class="actions">
      <Button type="submit" variant="primary" disabled={busy || targetId === ''}>{t('bindings.submit')}</Button>
    </div>
  </form>

  {#if linked}
    <Alert tone="success" live="polite" title={t('bindings.linked')} />
  {/if}
  {#if refusal}
    <ProblemAlert problem={refusal} level={3} />
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
  .bindings {
    display: grid;
    gap: var(--space-4);
    justify-items: start;
    margin-top: var(--space-7);
  }
  .bindings > :global(*) {
    max-width: 100%;
  }
  .bindings h2 {
    margin: 0;
    inline-size: 100%;
    padding-bottom: var(--space-2);
    border-bottom: var(--rule-hair) solid var(--color-ink);
    font-size: var(--step-2);
  }
  .bindings h3 {
    margin: 0;
    font-size: var(--step-1);
  }
  .bindings p {
    margin: 0;
  }
  .entries {
    inline-size: 100%;
  }
  .binding {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-4);
    padding: var(--space-3) 0;
  }
  .add {
    display: grid;
    gap: var(--space-4);
    inline-size: min(100%, var(--form-max));
  }
</style>
