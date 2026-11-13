<script lang="ts">
  import { onMount } from 'svelte';
  import { listProfiles, createProfile, updateProfile, removeProfile, setSourcePolicy } from '#lib/api/index.ts';
  import type { ProfileOut } from '#lib/api/types.gen.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { needsReauth, problemOf } from '#lib/triage.ts';

  let profiles = $state<ProfileOut[]>([]);
  let failed = $state(false);
  let message = $state('');
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  // new profile form
  let newName = $state('');
  let newRole = $state('member');
  let newAge = $state<number | null>(null);
  let newCanRate = $state(false);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function load(): Promise<void> {
    try {
      const { data } = await listProfiles({ throwOnError: true });
      profiles = data.items ?? data.profiles ?? [];
    } catch {
      failed = true;
      message = 'Erreur de chargement.';
    }
  }

  onMount(load);

  async function run(action: () => Promise<void>): Promise<void> {
    failed = false;
    message = '';
    try {
      await action();
      await load();
    } catch (error) {
      if (needsReauth(error)) {
        retry = () => run(action);
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        failed = true;
        message = 'Action refusée.';
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
          role: newRole,
          max_age_rating: newAge,
          can_rate: newCanRate
        },
        throwOnError: true
      });
      newName = '';
    });
  }

  function update(profile: ProfileOut, patch: Record<string, unknown>): void {
    void run(async () => {
      await updateProfile({
        headers: headers(),
        path: { profile_id: profile.id },
        body: patch,
        throwOnError: true
      });
    });
  }

  function remove(profile: ProfileOut): void {
    void run(async () => {
      await removeProfile({
        headers: headers(),
        path: { profile_id: profile.id },
        throwOnError: true
      });
    });
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
  <title>Profils</title>
</svelte:head>

<main>
  <h1>Profils</h1>

  <section>
    <h2>Nouveau profil</h2>
    <form onsubmit={create}>
      <label for="profile-name">Nom</label>
      <input id="profile-name" bind:value={newName} required />
      <label for="profile-role">Rôle</label>
      <select id="profile-role" bind:value={newRole}>
        <option value="admin">admin</option>
        <option value="member">member</option>
        <option value="child">child</option>
      </select>
      <label for="profile-age">Âge maximal</label>
      <input id="profile-age" type="number" bind:value={newAge} />
      <label>
        <input type="checkbox" bind:checked={newCanRate} />
        Peut classer
      </label>
      <button type="submit">Créer</button>
    </form>
  </section>

  <section>
    <h2>Profils existants</h2>
    <ul>
      {#each profiles as profile (profile.id)}
        <li data-testid="profile-row">
          <span>{profile.username} — {profile.role}</span>
          <button type="button" onclick={() => remove(profile)}>Supprimer</button>
        </li>
      {/each}
    </ul>
  </section>

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}

  {#if message}
    <p role={failed ? 'alert' : 'status'} class:err={failed}>{message}</p>
  {/if}
</main>

<style>
  main { max-width: 48rem; margin: 0 auto; padding: 2rem 1rem; }
  .err { color: var(--color-err, #b00); }
</style>