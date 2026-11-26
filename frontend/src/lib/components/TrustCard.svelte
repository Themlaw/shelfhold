<script lang="ts">
  // The fiche de confiance of a source (EF-UX-011): three lines, Officiel / Communauté / Inconnu,
  // the server says which one the source has and what each line means. The card decides nothing.
  import { hasMessage, t } from '#lib/i18n/index.ts';
  import type { TrustCardOut } from '#lib/api/types.gen.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Icon from '#lib/ui/atoms/Icon.svelte';

  let { trust }: { trust: TrustCardOut | null } = $props();

  const levelLabel = (level: string) => {
    if (level === 'official') return t('trust.level.official');
    if (level === 'community') return t('trust.level.community');
    return t('trust.level.unknown');
  };

  const detailOf = (line: TrustCardOut['lines'][number]) => {
    const label = levelLabel(line.level);
    if (line.label.trim().toLocaleLowerCase() === label.trim().toLocaleLowerCase()) {
      const key = `trust.help.${line.level}`;
      return hasMessage(key) ? t(key) : line.label;
    }
    return line.label;
  };
</script>

{#if trust}
  <ul role="list" aria-label={t('trust.list')}>
    {#each trust.lines as line (line.level + line.label)}
      <li aria-current={line.current ? 'true' : undefined}>
        <span class="level"><Icon name={line.level === 'official' ? 'shield' : line.level === 'community' ? 'users' : 'help'} size="sm" />{levelLabel(line.level)}</span>
        <span class="label">{detailOf(line)}</span>
        {#if line.current}
          <Badge tone="accent" icon="check">{t('trust.current')}</Badge>
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
    border-top: var(--rule-hair) solid var(--color-rule);
  }
  li {
    display: flex;
    gap: var(--space-1) var(--space-3);
    align-items: baseline;
    flex-wrap: wrap;
    padding: var(--space-2) var(--space-3);
    border-bottom: var(--rule-hair) solid var(--color-rule);
  }
  li[aria-current='true'] {
    background: var(--color-accent-soft);
    border-inline-start: var(--rule-thick) solid var(--color-accent);
  }
  .level {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    min-width: 8rem;
    font-weight: 600;
  }
  .label {
    flex: 1 1 12rem;
    color: var(--color-muted);
  }
</style>
