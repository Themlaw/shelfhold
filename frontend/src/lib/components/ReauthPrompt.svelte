<script lang="ts">
  // "Confirmez votre identité" (EF-USR-022): asked in place, so the action in progress is not
  // lost. The methods come from the 401 (`reauth_methods`); the password is the default.
  import { oidcReauth, passkeyReauthOptions, reauth } from '#lib/api/index.ts';
  import { t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Icon from '#lib/ui/atoms/Icon.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import { csrfToken, getPasskey, passkeysSupported } from '#lib/passkeys.ts';

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
  <div class="reauth__head">
    <Icon name="lock" />
    <h2 id="reauth-title">{t('reauth.title')}</h2>
  </div>
  <p class="reauth__explain">{t('reauth.explain')}</p>
  {#if canProvider && providerFirst}
    <Button variant="primary" icon="external" disabled={busy} onclick={withProvider}>{t('reauth.provider')}</Button>
  {/if}
  {#if canPassword}
    <form class="reauth__form" onsubmit={withPassword}>
      <Field label={t('reauth.password')} id="reauth-password">
        {#snippet children(control)}
          <!-- svelte-ignore a11y_autofocus -->
          <Input
            {...control}
            type="password"
            autocomplete="current-password"
            bind:value={password}
            required
            autofocus
          />
        {/snippet}
      </Field>
      <div class="btn-row">
        <Button type="submit" variant="primary" disabled={busy} loading={busy}>{t('reauth.confirm')}</Button>
        <Button variant="ghost" onclick={oncancel}>{t('reauth.cancel')}</Button>
      </div>
    </form>
  {/if}
  {#if canPasskey}
    <Button icon="key" disabled={busy} onclick={withPasskey}>{t('reauth.passkey')}</Button>
  {/if}
  {#if canProvider && !providerFirst}
    <Button icon="external" disabled={busy} onclick={withProvider}>{t('reauth.provider')}</Button>
  {/if}
  {#if !canPassword}
    <div class="btn-row">
      <Button variant="ghost" onclick={oncancel}>{t('reauth.cancel')}</Button>
    </div>
  {/if}
  {#if failed}
    <p role="alert" class="reauth__failed"><Icon name="alert" size="sm" /> {t('reauth.failed')}</p>
  {/if}
</section>

<style>
  .reauth {
    display: grid;
    gap: var(--space-3);
    justify-items: start;
    margin-block: var(--space-5);
    padding: var(--space-4) var(--space-5);
    border: var(--rule-hair) solid var(--color-warn);
    border-left-width: var(--rule-thick);
    border-radius: var(--radius-sm);
    background: var(--color-surface);
    box-shadow: var(--shadow-1);
  }
  .reauth__head {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--color-warn);
  }
  h2 {
    margin: 0;
    font-size: var(--step-1);
    color: var(--color-ink);
  }
  .reauth__explain {
    margin: 0;
    color: var(--color-muted);
    max-width: var(--measure);
  }
  .reauth__form {
    display: grid;
    gap: var(--space-3);
    inline-size: min(100%, 24rem);
  }
  .reauth__failed {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    color: var(--color-err);
    font-weight: 600;
  }
</style>
