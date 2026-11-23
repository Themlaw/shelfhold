<script lang="ts">
  // EF-LEC-008: « Reprendre la lecture ». What the server gives for the profile of the session: per
  // series, the Unit being read or the one that comes next. The server filters (a child is shown no
  // series that is hidden from her): the screen shows what it is given, and says nothing when the
  // list is empty, whatever the reason. Each entry carries the address it opens — its own server's,
  // the one the profile's account is on (two servers of one kind are two addresses).
  import type { ContinueItemOut } from '#lib/api/types.gen.ts';
  import { hasMessage, t } from '#lib/i18n/index.ts';
  import { webAddress } from '#lib/links.ts';

  let { items }: { items: ContinueItemOut[] } = $props();

  // The kind of the destination, as the interface names it (the slug itself when it is unknown).
  const destinationName = (kind: string): string => {
    const key = `destination.name.${kind}`;
    return hasMessage(key) ? t(key) : kind;
  };
</script>

{#if items.length > 0}
  <section class="continue-reading" aria-labelledby="continue-title">
    <h2 id="continue-title">{t('home.continue_reading')}</h2>
    <ul role="list">
      {#each items as item (item.work_id)}
        {@const verb =
          item.target === 'kept_copy'
            ? t('home.continue_read')
            : t('home.continue_open_in', { destination: destinationName(item.target) })}
        {@const label = t('home.continue_open', { open: verb, title: item.title, number: item.number })}
        <li>
          <span class="title">{item.title}</span>
          <span class="unit">
            {t('home.continue_unit', { number: item.number })} ·
            {item.state === 'in_progress'
              ? t('home.continue_in_progress')
              : t('home.continue_next')}{item.approximate ? ` · ${t('home.continue_approximate')}` : ''}
          </span>
          {#if item.target === 'kept_copy' && item.carrier_type && item.carrier_id}
            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
            <a class="open" href={`/lire/${encodeURIComponent(item.carrier_type)}/${encodeURIComponent(item.carrier_id)}`} aria-label={label}>
              {t('home.continue_read')}
            </a>
          {:else if webAddress(item.open_url)}
            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the address of another application -->
            <a class="open" href={webAddress(item.open_url)} rel="noopener noreferrer" aria-label={label}>
              {verb}
            </a>
          {/if}
        </li>
      {/each}
    </ul>
  </section>
{/if}

<style>
  .continue-reading h2 {
    font-size: 1.125rem;
    margin: 2rem 0 0.5rem;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.25rem 0.75rem;
    min-height: 2.75rem;
  }
  .title {
    font-weight: 600;
  }
  .unit {
    color: var(--color-muted);
  }
  .open {
    margin-left: auto;
    color: var(--color-accent);
    text-decoration: underline;
    min-height: 2.75rem;
    min-width: 2.75rem;
    display: inline-flex;
    align-items: center;
  }
</style>
