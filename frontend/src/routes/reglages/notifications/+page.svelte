<script lang="ts">
  // The notification preferences of the signed-in person (L14): `GET|PUT /api/v1/me/notifications`.
  // The whole of it is replaced at once; no quiet hours go as null, never as an empty text.
  import { onMount } from 'svelte';
  import { getNotifications, putNotifications } from '#lib/api/index.ts';
  import type { NotificationPrefsOut } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

  const MODES = ['digest', 'immediate'] as const;

  let prefs = $state<NotificationPrefsOut | null>(null);
  let notifyMode = $state<string>('digest');
  let quietStart = $state('');
  let quietEnd = $state('');
  let timezone = $state('');
  let failed = $state(false);
  let message = $state('');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const orNull = (text: string): string | null => (text.trim() === '' ? null : text.trim());

  function show(data: NotificationPrefsOut): void {
    prefs = data;
    notifyMode = data.notify_mode;
    quietStart = data.quiet_start ?? '';
    quietEnd = data.quiet_end ?? '';
    timezone = data.timezone;
  }

  async function load(): Promise<void> {
    try {
      const { data } = await getNotifications({ throwOnError: true });
      show(data);
    } catch {
      failed = true;
      message = t('notifications.error');
    }
  }

  onMount(load);

  async function save(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    failed = false;
    message = '';
    try {
      const { data } = await putNotifications({
        headers: headers(),
        body: {
          notify_mode: notifyMode,
          quiet_start: orNull(quietStart),
          quiet_end: orNull(quietEnd),
          timezone
        },
        throwOnError: true
      });
      show(data);
      message = t('notifications.saved');
    } catch {
      failed = true;
      message = t('notifications.refused');
    }
  }
</script>

<svelte:head>
  <title>{t('notifications.title')}</title>
</svelte:head>

<main class="page">
  <PageHead
    title={t('notifications.title')}
    crumbs={[{ label: t('settings.title'), href: '/reglages' }, { label: t('notifications.title') }]}
  />
  {#if prefs}
    <form class="stack page--form" onsubmit={save}>
      <Field label={t('notifications.mode')}>
        {#snippet children(control)}
          <Select
            {...control}
            bind:value={notifyMode}
            options={MODES.map((mode) => ({ value: mode, label: t(`notifications.mode.${mode}`) }))}
          />
        {/snippet}
      </Field>
      <Field label={t('notifications.quiet_start')}>
        {#snippet children(control)}
          <Input {...control} type="time" width="short" bind:value={quietStart} />
        {/snippet}
      </Field>
      <Field label={t('notifications.quiet_end')}>
        {#snippet children(control)}
          <Input {...control} type="time" width="short" bind:value={quietEnd} />
        {/snippet}
      </Field>
      <Field label={t('notifications.timezone')} required>
        {#snippet children(control)}
          <Input {...control} width="mid" bind:value={timezone} />
        {/snippet}
      </Field>
      <div class="btn-row">
        <Button type="submit" variant="primary">{t('notifications.save')}</Button>
      </div>
    </form>
  {:else if !failed}
    <div aria-busy="true"><Skeleton /><Skeleton /><Skeleton short /></div>
  {/if}
  {#if message}
    <div class="notice">
      <Alert
        tone={failed ? 'danger' : 'success'}
        live={failed ? 'assertive' : 'polite'}
        title={message}
      />
    </div>
  {/if}
</main>

<style>
  .notice {
    max-width: var(--form-max);
    margin-top: var(--space-4);
  }
</style>
