<script lang="ts">
  // The fiche de confiance of a source (EF-UX-011): three lines, Officiel / Communauté / Inconnu,
  // the server says which one the source has and what each line means. The card decides nothing.
  import { t } from '#lib/i18n/index.ts';
  import type { TrustCardOut } from '#lib/api/types.gen.ts';

  let { trust }: { trust: TrustCardOut | null } = $props();

  const levelLabel = (level: string) => {
    if (level === 'official') return t('trust.level.official');
    if (level === 'community') return t('trust.level.community');
    return t('trust.level.unknown');
  };
</script>

{#if trust}
  <ul role="list" aria-label={t('trust.list')}>
    {#each trust.lines as line (line.level + line.label)}
      <li aria-current={line.current ? 'true' : undefined}>
        <span class="level">{levelLabel(line.level)}</span>
        <span class="label">{line.label}</span>
        {#if line.current}
          <span class="current">{t('trust.current')}</span>
        {/if}
      </li>
    {/each}
  </ul>
{:else}
  <p>{t('trust.unavailable')}</p>
{/if}

<style>
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    gap: 0.25rem;
  }
  li {
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    flex-wrap: wrap;
  }
  .level {
    font-weight: 600;
  }
  .current {
    color: var(--color-muted);
    font-size: 0.875rem;
  }
</style>
