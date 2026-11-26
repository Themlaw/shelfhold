<script lang="ts">
  // "Connect a device" (EF-UX-004): the entry points of Kavita (Kover, KOReader by kosync or
  // OPDS, an OPDS e-reader), written and as a QR drawn here. Addresses only: the server gives no
  // key or token, the page asks for none, and an address that is not a web address draws no code.
  import { onMount } from 'svelte';
  import { deviceConnect } from '#lib/api/index.ts';
  import type { DeviceSkippedOut, DeviceTargetOut } from '#lib/api/types.gen.ts';
  import QrCode from '#lib/components/QrCode.svelte';
  import { Guard } from '#lib/guard.svelte.ts';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import ErrorState, { type ErrorKind } from '#lib/ui/molecules/ErrorState.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';
  import { notify } from '#lib/ui/toast.svelte.ts';
  import { webAddress } from '#lib/links.ts';

  type Entry = { id: string; what: MessageKey; url: string };

  const guard = new Guard();
  let targets = $state<DeviceTargetOut[]>([]);
  let skipped = $state<DeviceSkippedOut[]>([]);
  let loaded = $state(false);

  onMount(() =>
    guard.run(async () => {
      const { data } = await deviceConnect({ throwOnError: true });
      targets = data.targets;
      skipped = data.skipped ?? [];
      loaded = true;
    })
  );

  /** The four ways in, in the order of a person's day; one that is not a web address is dropped. */
  function entries(target: DeviceTargetOut): Entry[] {
    const all: Array<[string, MessageKey, string]> = [
      ['kover', 'devices.kover', target.kover],
      ['kosync', 'devices.koreader_kosync', target.kosync],
      ['koreader-opds', 'devices.koreader_opds', target.opds],
      ['reader-opds', 'devices.reader_opds', target.opds]
    ];
    const found: Entry[] = [];
    for (const [id, what, raw] of all) {
      const url = webAddress(raw);
      if (url) found.push({ id, what, url });
    }
    return found;
  }

  const MESSAGES = {
    signed_out: 'devices.signed_out',
    forbidden: 'devices.forbidden',
    failed: 'devices.error'
  } as const;
  const TITLES = {
    signed_out: 'devices.signed_out_title',
    forbidden: 'devices.forbidden_title',
    failed: 'devices.failed_title'
  } as const;
  const KINDS = { signed_out: 'signed-out', forbidden: 'forbidden', failed: 'server' } as const satisfies Record<string, ErrorKind>;

  async function copy(url: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(url);
      notify({ title: t('ui.secret.copied', { what: url }) });
    } catch {
      notify({ title: t('ui.secret.copy_failed'), tone: 'warning' });
    }
  }
</script>

<svelte:head>
  <title>{t('devices.title')}</title>
</svelte:head>

<main class="page">
  <PageHead title={t('devices.title')} lede={guard.state === 'ok' ? t('devices.lede') : undefined}>
    {#snippet actions()}
      {#if guard.state === 'ok'}
        <Button href="/appareils/jetons" icon="device">{t('deviceTokens.title')}</Button>
      {/if}
    {/snippet}
  </PageHead>

  {#if guard.state !== 'ok'}
    <div role="alert">
    <ErrorState kind={KINDS[guard.state]} title={t(TITLES[guard.state])} headingLevel={2}>
      {t(MESSAGES[guard.state])}
    </ErrorState>
    </div>
  {:else}
    <p class="hint key-note">{t('devices.key_note')}</p>

    {#each skipped as left (left.target_id)}
      <div class="skipped">
        <Alert tone="warning" title={t('devices.skipped_title')} live="polite">
          <p>{t('devices.skipped', { name: left.name })}</p>
        </Alert>
      </div>
    {/each}

    {#if !loaded}
      <div aria-busy="true">
        <p class="visually-hidden" role="status">{t('devices.loading')}</p>
        <Skeleton /><Skeleton short />
      </div>
    {:else if targets.length === 0 && skipped.length === 0}
      <EmptyState title={t('devices.none')} />
    {/if}

    {#each targets as target (target.target_id)}
      <section class="section" aria-labelledby={`target-${target.target_id}`}>
        <div class="section-head">
          <h2 id={`target-${target.target_id}`}>{t('devices.target', { name: target.name })}</h2>
        </div>
        <ul class="cards" role="list">
          {#each entries(target) as entry (entry.id)}
            <li class="card panel">
              <h3 class="card__title">{t(entry.what)}</h3>
              <QrCode text={entry.url} label={t('devices.qr_label', { what: t(entry.what), url: entry.url })} />
              <code class="card__url">{entry.url}</code>
              <Button size="sm" icon="copy" aria-label={`${t('devices.copy_address')} : ${t(entry.what)}`} onclick={() => copy(entry.url)}>
                {t('ui.secret.copy_short')}
              </Button>
            </li>
          {/each}
        </ul>
      </section>
    {/each}
  {/if}
</main>

<style>
  .key-note {
    max-width: var(--measure);
    margin-top: calc(-1 * var(--space-3));
  }
  .skipped {
    margin-bottom: var(--space-4);
  }
  .cards {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 15rem), 1fr));
    gap: var(--space-5);
  }
  .card {
    display: grid;
    gap: var(--space-3);
    justify-items: start;
    min-width: 0;
  }
  .card__title {
    margin: 0;
    font-size: var(--step-1);
  }
  .card__url {
    max-width: 100%;
    overflow-wrap: anywhere;
    word-break: break-all;
    font-size: var(--step--1);
    color: var(--color-muted);
  }
</style>
