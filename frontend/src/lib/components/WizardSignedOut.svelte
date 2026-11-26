<script lang="ts">
  // « You are not signed in »: the way out is the sign-in page, served by the backend (a full
  // load, outside the app).
  import { t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
</script>

<Alert tone="warning" title={t('admin.wizard.signed_out')} live="assertive">
  {#snippet actions()}
    <Button href="/login" data-sveltekit-reload icon="user" size="sm">{t('admin.wizard.login')}</Button>
  {/snippet}
</Alert>
