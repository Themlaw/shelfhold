<script lang="ts">
  // The admin's queue and maintenance screen (EF-INT-017, EF-INT-018). The role is checked by the
  // server on every call; the component shows its refusal and nothing else.
  import QueueControls from '#lib/components/QueueControls.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { navItems } from '#lib/nav.ts';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

  const activityHref = navItems(['activity'])[0].href;
</script>

<svelte:head>
  <title>{t('queue.title')}</title>
</svelte:head>

<main class="page">
  <PageHead
    title={t('queue.title')}
    crumbs={[{ label: t('nav.activity'), href: activityHref }, { label: t('queue.title') }]}
  />
  <QueueControls />
</main>
