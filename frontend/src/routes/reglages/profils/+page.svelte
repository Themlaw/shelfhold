<script lang="ts">
  // Profile screens (EF-USR-001, EF-USR-016): the administrator's, nobody else's. The server is the
  // gate (a member, a child and a key are refused, a stale session is asked to prove again); this
  // screen only shows what it was given and asks for the password in place when told to.
  import { onMount } from 'svelte';
  import {
    createProfile,
    listProfiles,
    removeProfile,
    setSourcePolicy,
    setVisibility,
    sourcePolicies,
    updateProfile
  } from '#lib/api/index.ts';
  import type { PolicyOut, ProfileOut } from '#lib/api/types.gen.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { t, formatNumber, type MessageKey } from '#lib/i18n/index.ts';
  import Avatar from '#lib/ui/atoms/Avatar.svelte';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Checkbox from '#lib/ui/atoms/Checkbox.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Segmented from '#lib/ui/atoms/Segmented.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Switch from '#lib/ui/atoms/Switch.svelte';
  import AgeScale, { type AgeStep } from '#lib/ui/molecules/AgeScale.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import ConfirmDialog from '#lib/ui/molecules/ConfirmDialog.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import ErrorState from '#lib/ui/molecules/ErrorState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import SettingRow from '#lib/ui/molecules/SettingRow.svelte';
  import type { Column } from '#lib/ui/molecules/TableRow.svelte';
  import DataTable from '#lib/ui/organisms/DataTable.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { isForbidden, needsReauth, problemOf } from '#lib/triage.ts';

  const ROLES = ['admin', 'member', 'child'] as const;
  const VISIBILITIES = ['private', 'household'] as const;

  let profiles = $state<ProfileOut[]>([]);
  let policies = $state<Record<string, PolicyOut[]>>({});
  let adminOnly = $state(false);
  let failed = $state(false);
  let loaded = $state(false);
  let removing = $state<ProfileOut | null>(null);
  let removeOpen = $state(false);
  let message = $state('');
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  // One editor at a time: the chosen profile, or the creation form (`creating`).
  let selectedId = $state<string | null>(null);
  let creating = $state(false);
  const selected = $derived(profiles.find((profile) => profile.id === selectedId) ?? null);
  const COLUMNS: readonly Column[] = [
    { key: 'username', label: t('profiles.name'), primary: true },
    { key: 'role', label: t('profiles.role') },
    { key: 'age', label: t('profiles.age') },
    { key: 'visibility', label: t('profiles.visibility') }
  ];

  let newName = $state('');
  let newPassword = $state('');
  let newRole = $state<(typeof ROLES)[number]>('member');
  let newAge = $state<number | null>(null);
  let newCanRate = $state(false);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function load(): Promise<void> {
    try {
      const { data } = await listProfiles({ throwOnError: true });
      profiles = data.items;
      if (!creating && !profiles.some((profile) => profile.id === selectedId)) selectedId = profiles[0]?.id ?? null;
    } catch (error) {
      failed = true;
      adminOnly = isForbidden(error);
      message = adminOnly ? t('profiles.admin_only') : t('profiles.error');
      return;
    }
    const read: Record<string, PolicyOut[]> = {};
    for (const profile of profiles) {
      try {
        const { data } = await sourcePolicies({
          path: { profile_id: profile.id },
          throwOnError: true
        });
        read[profile.id] = data.items;
      } catch {
        read[profile.id] = [];
      }
    }
    policies = read;
  }

  async function first(): Promise<void> {
    await load();
    loaded = true;
  }

  // The marks of the age rule: the ceilings the library knows, plus the child's own when it is
  // none of them (a ceiling of 7 must not be shown as another one).
  const MARKS = [0, 10, 12, 13, 16, 18];
  function steps(current: number | null): AgeStep[] {
    const values = current !== null && !MARKS.includes(current) ? [...MARKS, current].sort((a, b) => a - b) : MARKS;
    return [
      ...values.map((value) => ({ value, label: value === 0 ? t('age.0') : `${formatNumber(value)}+` })),
      { value: null, label: t('profiles.age_none') }
    ];
  }
  const ageText = (age: number | null) => (age === null ? t('profiles.age_none') : age === 0 ? t('age.0') : `${formatNumber(age)}+`);
  const roleName = (role: string) => t(`profiles.role.${role}` as MessageKey);
  const kindOf = (role: string): 'admin' | 'child' | 'adult' => (role === 'admin' ? 'admin' : role === 'child' ? 'child' : 'adult');

  const askRemove = (profile: ProfileOut) => {
    removing = profile;
    removeOpen = true;
  };

  onMount(first);

  async function run(action: () => Promise<void>, done: string): Promise<void> {
    failed = false;
    message = '';
    try {
      await action();
      await load();
      if (!failed) message = done; // a failed reload says so instead
    } catch (error) {
      if (needsReauth(error)) {
        retry = () => run(action, done);
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        failed = true;
        message = t('profiles.refused');
      }
    }
  }

  function create(event: SubmitEvent): void {
    event.preventDefault();
    void run(async () => {
      const { data: made } = await createProfile({
        headers: headers(),
        body: {
          username: newName,
          password: newPassword,
          role: newRole,
          max_age_rating: newAge,
          can_rate: newCanRate
        },
        throwOnError: true
      });
      newName = '';
      newPassword = '';
      newAge = null;
      selectedId = made.id;
      creating = false;
    }, t('profiles.created'));
  }

  function update(profile: ProfileOut, patch: { max_age_rating?: number | null; can_rate?: boolean }): void {
    void run(async () => {
      await updateProfile({
        headers: headers(),
        path: { profile_id: profile.id },
        body: patch,
        throwOnError: true
      });
    }, t('profiles.saved'));
  }

  function chooseVisibility(profile: ProfileOut, visibility: 'private' | 'household'): void {
    void run(async () => {
      await setVisibility({
        headers: headers(),
        path: { profile_id: profile.id },
        body: { visibility },
        throwOnError: true
      });
    }, t('profiles.saved'));
  }

  function choosePolicy(profile: ProfileOut, source: string, policy: string): void {
    void run(async () => {
      await setSourcePolicy({
        headers: headers(),
        path: { profile_id: profile.id, source_key: source },
        body: { policy },
        throwOnError: true
      });
    }, t('profiles.saved'));
  }

  function remove(profile: ProfileOut): void {
    void run(async () => {
      await removeProfile({
        headers: headers(),
        path: { profile_id: profile.id },
        throwOnError: true
      });
    }, t('profiles.removed'));
  }


  function afterReauth(): void {
    const again = retry;
    retry = null;
    reauthMethods = null;
    if (again) void again();
  }

  function cancelReauth(): void {
    retry = null;
    reauthMethods = null;
  }
</script>

<svelte:head>
  <title>{t('profiles.title')}</title>
</svelte:head>

<main class="page">
  <div class="running-head">
    <span>{t('nav.group.family')}</span>
    {#if loaded && !adminOnly && !failed}<span class="running-head__folio">{t('profiles.count', { count: profiles.length })}</span>{/if}
  </div>
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('profiles.title')}</h1>
      <p class="lede">{t('profiles.lede')}</p>
    </div>
    {#if loaded && !adminOnly && !(failed && profiles.length === 0)}
      <div class="page-head__actions">
        <Button variant="primary" icon="plus" onclick={() => (creating = true)}>{t('profiles.new')}</Button>
      </div>
    {/if}
  </div>

  {#if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('profiles.loading')}</p>
      <Skeleton /><Skeleton /><Skeleton short />
    </div>
  {:else if adminOnly}
    <ErrorState kind="forbidden" title={t('profiles.admin_only_title')} headingLevel={2}>
      {t('profiles.admin_only')}
    </ErrorState>
  {:else if failed && profiles.length === 0}
    <ErrorState kind="server" title={t('profiles.error_title')} headingLevel={2}>
      {message}
      {#snippet actions()}<Button icon="refresh" onclick={() => void first()}>{t('error.retry')}</Button>{/snippet}
    </ErrorState>
  {:else}
    <section class="section" aria-labelledby="profiles-existing">
      <div class="section-head">
        <h2 id="profiles-existing">{t('profiles.existing')}</h2>
      </div>
      <DataTable
        caption={t('profiles.existing')}
        captionHidden
        columns={COLUMNS}
        rows={profiles.map((profile) => ({ ...profile }))}
        key={(row) => String(row.id)}
        selected={(row) => !creating && row.id === selectedId}
      >
        {#snippet empty()}
          <EmptyState title={t('profiles.empty')} headingLevel={3} art={false} />
        {/snippet}
        {#snippet cell(column, row)}
          {@const profile = row as unknown as ProfileOut}
          {#if column.key === 'username'}
            <button
              type="button"
              class="pick"
              data-testid="profile-row"
              aria-pressed={!creating && profile.id === selectedId}
              onclick={() => {
                creating = false;
                selectedId = profile.id;
              }}
            >
              <Avatar name={profile.username} kind={kindOf(profile.role)} size="sm" />
              <span>{profile.username}</span>
            </button>
          {:else if column.key === 'role'}
            {roleName(profile.role)}
          {:else if column.key === 'age'}
            {#if profile.role === 'child'}
              <Badge tone="age" label={t('profiles.age_current', { age: ageText(profile.max_age_rating) })}>{ageText(profile.max_age_rating)}</Badge>
            {:else}
              <span class="muted" aria-hidden="true">—</span>
            {/if}
          {:else}
            {t(`profiles.visibility.${profile.visibility}` as MessageKey)}
          {/if}
        {/snippet}
      </DataTable>
    </section>

    {#if creating}
      <section class="section editor" aria-labelledby="profiles-new">
        <div class="section-head">
          <h2 id="profiles-new">{t('profiles.new')}</h2>
        </div>
        <p class="hint">{t('profiles.new_help')}</p>
        <form class="new" onsubmit={create} aria-labelledby="profiles-new">
          <Field label={t('profiles.name')} required>
            {#snippet children(control)}<Input {...control} bind:value={newName} autocomplete="off" />{/snippet}
          </Field>
          <Field label={t('profiles.password')} required>
            {#snippet children(control)}
              <Input {...control} type="password" bind:value={newPassword} autocomplete="new-password" />
            {/snippet}
          </Field>
          <Field label={t('profiles.role')}>
            {#snippet children(control)}
              <Select {...control} bind:value={newRole} options={ROLES.map((value) => ({ value, label: roleName(value) }))} />
            {/snippet}
          </Field>
          <Field label={t('profiles.age')}>
            {#snippet children(control)}<Input {...control} type="number" min="0" bind:value={newAge} width="short" />{/snippet}
          </Field>
          <Checkbox bind:checked={newCanRate} label={t('profiles.can_rate')} />
          <!-- direct children of the form: on a phone the main action of a long form stays in reach -->
          <Button type="submit" variant="primary" icon="plus" class="main-action new__submit">{t('profiles.create')}</Button>
          <Button variant="ghost" class="new__cancel" onclick={() => (creating = false)}>{t('profiles.cancel')}</Button>
        </form>
      </section>
    {:else if selected}
      {@const profile = selected}
      <section class="section editor" aria-labelledby="profile-editor-title" data-testid="profile-editor">
        <header class="profile__head">
          <Avatar name={profile.username} kind={kindOf(profile.role)} size="lg" />
          <div class="profile__who">
            <h2 id="profile-editor-title" class="profile__name">{profile.username}</h2>
            <p class="profile__role">{roleName(profile.role)}</p>
          </div>
        </header>

        <div class="settings">
          {#if profile.role === 'child'}
            <SettingRow label={t('profiles.age')} help={t('profiles.age_help')}>
              <AgeScale
                name={`age-${profile.id}`}
                legend={t('profiles.age')}
                hideLegend
                steps={steps(profile.max_age_rating)}
                value={profile.max_age_rating}
                onchange={(age) => update(profile, { max_age_rating: age })}
              />
            </SettingRow>
          {:else}
            <div class="settings__row">
              <Switch
                label={t('profiles.can_rate')}
                checked={profile.can_rate}
                onchange={(e) => update(profile, { can_rate: (e.target as HTMLInputElement).checked })}
              />
            </div>
          {/if}

          <SettingRow label={t('profiles.visibility')} help={t('profiles.visibility_help')}>
            {#snippet children(control)}
              <Select
                {...control}
                value={profile.visibility}
                options={VISIBILITIES.map((value) => ({ value, label: t(`profiles.visibility.${value}`) }))}
                onchange={(e) =>
                  chooseVisibility(profile, (e.target as HTMLSelectElement).value as 'private' | 'household')}
              />
            {/snippet}
          </SettingRow>

          <SettingRow label={t('profiles.policy_title', { name: profile.username })} help={t('profiles.policy_help')}>
            {#if (policies[profile.id] ?? []).length === 0}
              <p class="hint">{t('profiles.policy_none')}</p>
            {:else}
              <ul class="policies" role="list">
                {#each policies[profile.id] ?? [] as policy (policy.source_key)}
                  <li class="policy">
                    <span class="policy__source" aria-hidden="true">{policy.source_key}</span>
                    <Segmented
                      name={`policy-${profile.id}-${policy.source_key}`}
                      legend={t('profiles.policy', { source: policy.source_key })}
                      hideLegend
                      value={policy.policy}
                      options={[
                        { value: 'add_directly', label: t('profiles.policy.add_directly'), icon: 'plus' },
                        { value: 'request', label: t('profiles.policy.request'), icon: 'inbox' },
                        { value: 'forbidden', label: t('profiles.policy.forbidden'), icon: 'x' }
                      ]}
                      onchange={(value) => choosePolicy(profile, policy.source_key, value)}
                    />
                  </li>
                {/each}
              </ul>
            {/if}
          </SettingRow>
        </div>

        <div class="danger">
          <div>
            <p class="danger__title">{t('profiles.danger')}</p>
            <p class="hint">{t('profiles.remove_help')}</p>
          </div>
          <Button variant="danger-ghost" icon="trash" onclick={() => askRemove(profile)}>{t('profiles.remove')}</Button>
        </div>
      </section>
    {/if}
  {/if}

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}

  {#if message && loaded && !adminOnly && !(failed && profiles.length === 0)}
    <div class="notice">
      <Alert tone={failed ? 'danger' : 'success'} title={message} live={failed ? 'assertive' : 'polite'} />
    </div>
  {/if}
</main>

{#if removing}
  <ConfirmDialog
    bind:open={removeOpen}
    title={t('profiles.remove_title', { name: removing.username })}
    message={t('profiles.remove_help')}
    confirmLabel={t('profiles.remove_confirm')}
    onconfirm={() => {
      const target = removing;
      if (target) remove(target);
    }}
  />
{/if}

<style>
  .editor {
    display: grid;
    gap: var(--space-4);
  }
  .pick {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    min-block-size: 44px;
    padding: 0;
    border: 0;
    background: none;
    color: var(--color-link, var(--color-accent-text));
    font: 600 var(--step-0) / 1.2 var(--font-text);
    text-decoration: underline;
    text-underline-offset: 0.2em;
    cursor: pointer;
  }
  .pick[aria-pressed='true'] {
    color: var(--color-ink);
    text-decoration-thickness: var(--rule-thick);
  }
  .pick:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }
  .muted {
    color: var(--color-muted);
  }
  .new :global(.new__submit),
  .new :global(.new__cancel) {
    align-self: flex-start;
  }
  .profile__head {
    display: flex;
    align-items: center;
    gap: var(--space-4);
  }
  .profile__name {
    margin: 0;
    font-size: var(--step-2);
  }
  .profile__role {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    color: var(--color-muted);
    font-style: italic;
  }
  .settings {
    display: grid;
    border-top: var(--rule-hair) solid var(--color-ink);
  }
  .settings > :global(.setting),
  .settings__row {
    padding-block: var(--space-4);
    border-bottom: var(--rule-hair) solid var(--color-rule);
  }
  .policies {
    display: grid;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .policy {
    display: grid;
    grid-template-columns: minmax(6rem, 1fr) minmax(0, 2fr);
    align-items: center;
    gap: var(--space-3);
  }
  .policy__source {
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  .danger {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
    border: var(--rule-hair) solid var(--color-err);
    border-radius: var(--radius-sm);
  }
  .danger__title {
    margin: 0;
    font-weight: 700;
    color: var(--color-err);
  }
  .danger .hint {
    margin: 0;
    max-width: var(--measure);
  }
  .new {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    max-inline-size: var(--form-max);
  }
  .notice {
    position: sticky;
    bottom: calc(var(--tabbar-h, 0px) + var(--space-3));
    margin-top: var(--space-5);
  }
  @media (max-width: 40rem) {
    .policy {
      grid-template-columns: minmax(0, 1fr);
      gap: var(--space-1);
    }
  }
</style>
