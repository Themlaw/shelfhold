<script lang="ts">
  // "Confirmez votre identité" (EF-USR-022): asked in place, so the action in progress is not
  // lost. The methods come from the 401 (`reauth_methods`); the password is the default.
  import { oidcReauth, passkeyReauthOptions, reauth } from '#lib/api/index.ts';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken, getPasskey, passkeysSupported } from '#lib/passkeys.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';

  let {
    methods = ['password'],
    ondone,
    oncancel,
    onprovider
  }: {
    methods?: string[];
    ondone: () => void | Promise<void>;
    oncancel: () => void;
    /** The identity provider takes the browser away: the caller notes what to take up on return. */
    onprovider?: () => void;
  } = $props();

  let password = $state('');
  let failed = $state(false);
  let busy = $state(false);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const canPassword = $derived(methods.length === 0 || methods.includes('password'));
  const canPasskey = $derived(methods.includes('passkey') && passkeysSupported());
  const canProvider = $derived(methods.includes('oidc'));
  // the server puts the provider first for a session it opened (no local proof), last for another
  const providerFirst = $derived(methods[0] === 'oidc');

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

  /** The person signs in again at the provider (`prompt=login`, `max_age`): the page is left, and
   * the settings screen takes the action up when the provider sends the browser back. */
  const withProvider = async (): Promise<void> => {
    busy = true;
    failed = false;
    try {
      const { data } = await oidcReauth({ headers: headers(), throwOnError: true });
      onprovider?.();
      window.location.assign(data.authorization_url);
    } catch {
      failed = true;
    } finally {
      busy = false;
    }
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
  <p class="muted">{t('reauth.explain')}</p>
  {#if canProvider && providerFirst}
    <Button loading={busy} onclick={withProvider}>{t('reauth.provider')}</Button>
  {/if}
  {#if canPassword}
    <form onsubmit={withPassword}>
      <Field label={t('reauth.password')} id="reauth-password">
        {#snippet children(control)}
          <!-- svelte-ignore a11y_autofocus -->
          <Input {...control} type="password" autocomplete="current-password" bind:value={password} required autofocus />
        {/snippet}
      </Field>
      <Button type="submit" variant="primary" loading={busy}>{t('reauth.confirm')}</Button>
    </form>
  {/if}
  {#if canPasskey}
    <Button icon="key" loading={busy} onclick={withPasskey}>{t('reauth.passkey')}</Button>
  {/if}
  {#if canProvider && !providerFirst}
    <Button loading={busy} onclick={withProvider}>{t('reauth.provider')}</Button>
  {/if}
  <div><Button variant="ghost" onclick={oncancel}>{t('reauth.cancel')}</Button></div>
  {#if failed}
    <Alert tone="danger" title={t('reauth.failed')} live="assertive" />
  {/if}
</section>

<style>
  .reauth {
    display: grid;
    gap: var(--space-3);
    justify-items: start;
    margin: var(--space-4) 0;
    padding: var(--space-5);
    border: var(--rule-hair) solid var(--color-warn);
    border-left-width: var(--rule-thick);
    border-radius: var(--radius-md);
    background: var(--color-surface);
  }
  .reauth > :global(*) {
    max-width: 100%;
  }
  h2 {
    margin: 0;
    font-size: var(--step-1);
  }
  p {
    margin: 0;
  }
  form {
    display: grid;
    gap: var(--space-3);
    justify-items: start;
    inline-size: min(100%, 24rem);
  }
</style>
