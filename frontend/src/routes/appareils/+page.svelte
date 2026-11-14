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
</script>

<svelte:head>
  <title>{t('devices.title')}</title>
</svelte:head>

<main>
  <h1>{t('devices.title')}</h1>

  {#if guard.state !== 'ok'}
    <p role="alert" class="err">{t(MESSAGES[guard.state])}</p>
  {:else}
    <p>{t('devices.intro')}</p>
    <p class="muted">{t('devices.key_note')}</p>

    {#each skipped as left (left.target_id)}
      <p role="status" class="err">{t('devices.skipped', { name: left.name })}</p>
    {/each}

    {#if loaded && targets.length === 0 && skipped.length === 0}
      <p>{t('devices.none')}</p>
    {/if}

    {#each targets as target (target.target_id)}
      <section aria-label={t('devices.target', { name: target.name })}>
        <h2>{t('devices.target', { name: target.name })}</h2>
        <ul class="cards">
          {#each entries(target) as entry (entry.id)}
            <li>
              <strong>{t(entry.what)}</strong>
              <QrCode text={entry.url} label={t('devices.qr_label', { what: t(entry.what), url: entry.url })} />
              <code>{entry.url}</code>
            </li>
          {/each}
        </ul>
      </section>
    {/each}
  {/if}
</main>

<style>
  main {
    max-width: 48rem;
    margin: 0 auto;
    padding: 3rem 1rem;
  }
  h1 {
    font-family: var(--font-display);
    font-weight: 400;
    font-style: italic;
    letter-spacing: -0.02em;
  }
  .cards {
    list-style: none;
    margin: 1rem 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 14rem), 1fr));
    gap: 1rem;
  }
  .cards li {
    display: grid;
    gap: 0.5rem;
    justify-items: start;
    padding: 1rem;
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    background: var(--color-surface);
  }
  main,
  .cards li {
    min-width: 0;
  }
  .cards li > * {
    max-width: 100%;
  }
  code,
  strong,
  p {
    overflow-wrap: anywhere;
  }
  .muted {
    color: var(--color-muted);
  }
  .err {
    color: var(--color-err);
  }
</style>
