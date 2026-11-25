<script lang="ts">
  // The one way forward of a finished step.
  import { t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';

  let { href }: { href: string } = $props();
</script>

<div class="btn-row">
  <Button {href} variant="primary" size="lg" iconEnd="right">{t('admin.wizard.next')}</Button>
</div>
