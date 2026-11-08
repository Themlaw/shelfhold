<script lang="ts">
  // "Confirmez votre identité" (EF-USR-022): asked in place, so the action in progress is not
  // lost. The methods come from the 401 (`reauth_methods`); the password is the default.
  import { passkeyReauthOptions, reauth } from '#lib/api/index.ts';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken, getPasskey, passkeysSupported } from '#lib/passkeys.ts';

  let {
    methods = ['password'],
    ondone,
    oncancel
  }: { methods?: string[]; ondone: () => void | Promise<void>; oncancel: () => void } = $props();

  let password = $state('');
  let failed = $state(false);
  let busy = $state(false);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const canPassword = $derived(methods.length === 0 || methods.includes('password'));
  const canPasskey = $derived(methods.includes('passkey') && passkeysSupported());

  async function submit(send: () => Promise<unknown>): Promise<void> {
    busy = true;
    failed = false;
    try {
      await send();
      password = '';
      await ondone();
    } catch {
      failed = true;
    } finally {
      busy = false;
    }
  }

  const withPassword = (event: SubmitEvent) => {
    event.preventDefault();
    return submit(() =>
      reauth({ headers: headers(), body: { method: 'password', password }, throwOnError: true })
    );
  };

  const withPasskey = () =>
    submit(async () => {
      const { data: started } = await passkeyReauthOptions({
        headers: headers(),
        throwOnError: true
      });
      const credential = await getPasskey(started.options);
      await reauth({
        headers: headers(),
        body: { method: 'passkey', flow: started.flow, credential },
        throwOnError: true
      });
    });
</script>

<section class="reauth" aria-labelledby="reauth-title">
  <h2 id="reauth-title">{t('reauth.title')}</h2>
  <p>{t('reauth.explain')}</p>
  {#if canPassword}
    <form onsubmit={withPassword}>
      <label for="reauth-password">{t('reauth.password')}</label>
      <!-- svelte-ignore a11y_autofocus -->
      <input
        id="reauth-password"
        type="password"
        autocomplete="current-password"
        bind:value={password}
        required
        autofocus
      />
      <button type="submit" disabled={busy}>{t('reauth.confirm')}</button>
    </form>
  {/if}
  {#if canPasskey}
    <button type="button" disabled={busy} onclick={withPasskey}>{t('reauth.passkey')}</button>
  {/if}
  <button type="button" class="plain" onclick={oncancel}>{t('reauth.cancel')}</button>
  {#if failed}
    <p role="alert" class="err">{t('reauth.failed')}</p>
  {/if}
</section>

<style>
  .reauth {
    display: grid;
    gap: 0.5rem;
    margin: 1rem 0;
    padding: 1rem;
    border: 1px solid var(--color-warn);
    border-radius: var(--radius-cover);
    background: var(--color-surface);
  }
  h2 {
    margin: 0;
    font-size: 1.125rem;
  }
  form {
    display: grid;
    gap: 0.5rem;
  }
  .err {
    color: var(--color-err);
  }
</style>
