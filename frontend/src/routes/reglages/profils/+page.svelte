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
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { isForbidden, needsReauth, problemOf } from '#lib/triage.ts';

  const ROLES = ['admin', 'member', 'child'] as const;
  const VISIBILITIES = ['private', 'household'] as const;
  const POLICIES = ['add_directly', 'request', 'forbidden'] as const;

  let profiles = $state<ProfileOut[]>([]);
  let policies = $state<Record<string, PolicyOut[]>>({});
  let adminOnly = $state(false);
  let failed = $state(false);
  let message = $state('');
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

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

  onMount(load);

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
      await createProfile({
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

  const ageOf = (text: string): number | null => (text.trim() === '' ? null : Number(text));

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

<main>
  <h1>{t('profiles.title')}</h1>

  {#if !adminOnly}
    <section>
      <h2 id="profiles-new">{t('profiles.new')}</h2>
      <form onsubmit={create} aria-labelledby="profiles-new">
        <label for="profile-name">{t('profiles.name')}</label>
        <input id="profile-name" bind:value={newName} required autocomplete="off" />
        <label for="profile-password">{t('profiles.password')}</label>
        <input
          id="profile-password"
          type="password"
          bind:value={newPassword}
          required
          autocomplete="new-password"
        />
        <label for="profile-role">{t('profiles.role')}</label>
        <select id="profile-role" bind:value={newRole}>
          {#each ROLES as role (role)}
            <option value={role}>{role}</option>
          {/each}
        </select>
        <label for="profile-age">{t('profiles.age')}</label>
        <input id="profile-age" type="number" min="0" bind:value={newAge} />
        <label>
          <input type="checkbox" bind:checked={newCanRate} />
          {t('profiles.can_rate')}
        </label>
        <button type="submit">{t('profiles.create')}</button>
      </form>
    </section>

    <section>
      <h2>{t('profiles.existing')}</h2>
      <ul>
        {#each profiles as profile (profile.id)}
          <li data-testid="profile-row">
            <span>{profile.username} — {profile.role}</span>
            <label for={`vis-${profile.id}`}>{t('profiles.visibility')}</label>
            <select
              id={`vis-${profile.id}`}
              value={profile.visibility}
              onchange={(e) =>
                chooseVisibility(profile, (e.target as HTMLSelectElement).value as 'private' | 'household')}
            >
              {#each VISIBILITIES as visibility (visibility)}
                <option value={visibility}>{t(`profiles.visibility.${visibility}`)}</option>
              {/each}
            </select>
            {#if profile.role === 'child'}
              <label for={`age-${profile.id}`}>{t('profiles.age')}</label>
              <input
                id={`age-${profile.id}`}
                type="number"
                min="0"
                value={profile.max_age_rating ?? ''}
                onchange={(e) =>
                  update(profile, { max_age_rating: ageOf((e.target as HTMLInputElement).value) })}
              />
            {:else}
              <label>
                <input
                  type="checkbox"
                  checked={profile.can_rate}
                  onchange={(e) => update(profile, { can_rate: (e.target as HTMLInputElement).checked })}
                />
                {t('profiles.can_rate')}
              </label>
            {/if}
            {#each policies[profile.id] ?? [] as policy (policy.source_key)}
              <label for={`policy-${profile.id}-${policy.source_key}`}>
                {t('profiles.policy', { source: policy.source_key })}
              </label>
              <select
                id={`policy-${profile.id}-${policy.source_key}`}
                value={policy.policy}
                onchange={(e) =>
                  choosePolicy(profile, policy.source_key, (e.target as HTMLSelectElement).value)}
              >
                {#each POLICIES as option (option)}
                  <option value={option}>{t(`profiles.policy.${option}`)}</option>
                {/each}
              </select>
            {/each}
            <button type="button" onclick={() => remove(profile)}>{t('profiles.remove')}</button>
          </li>
        {/each}
      </ul>
    </section>
  {/if}

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}

  {#if message}
    <p role={failed ? 'alert' : 'status'} class:err={failed}>{message}</p>
  {/if}
</main>

<style>
  main {
    max-width: 48rem;
    margin: 0 auto;
    padding: 2rem 1rem;
  }
  form {
    display: grid;
    gap: 0.5rem;
    max-width: 24rem;
  }
  li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0;
  }
  .err {
    color: var(--color-err);
  }
</style>
