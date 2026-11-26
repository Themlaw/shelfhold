<script lang="ts">
  // Settings in three levels (EF-UX-009). The server decides what each row is: its level, whether
  // it is locked (set by the environment or fixed by this release) and whether it is secret. The
  // screen draws that and decides nothing, so a new setting needs no change here.
  import { onMount } from 'svelte';
  import { readConfig, patchConfig } from '#lib/api/index.ts';
  import type { ConfigRow } from '#lib/api/types.gen.ts';
  import { hasMessage, t } from '#lib/i18n/index.ts';
  import { isForbidden, needsReauth, problemOf } from '#lib/triage.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import Icon from '#lib/ui/atoms/Icon.svelte';
  import type { IconName } from '#lib/ui/atoms/icons.ts';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import SettingRow from '#lib/ui/molecules/SettingRow.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';
  import SettingsForm from '#lib/ui/organisms/SettingsForm.svelte';

  const LEVELS = ['essential', 'advanced', 'expert'] as const;

  let items = $state<ConfigRow[]>([]);
  let adminOnly = $state(false);
  let loaded = $state(false);
  let failed = $state(false);
  let message = $state('');
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  const originOf = (row: ConfigRow): string => row.overridden_by ?? row.source;
  const originText = (row: ConfigRow): string => {
    const key = `settings.origin.${originOf(row)}`;
    return t('settings.origin', { origin: hasMessage(key) ? t(key) : originOf(row) });
  };
  const helpOf = (row: ConfigRow): string | null => {
    const key = `settings.help.${row.key}`;
    return hasMessage(key) ? t(key) : null;
  };
  const labelOf = (row: ConfigRow): string => {
    const key = `settings.label.${row.key}`;
    if (hasMessage(key)) return t(key);
    if (row.key.startsWith('path_map.')) return `${t('settings.label.path_map')} — ${row.key.slice('path_map.'.length)}`;
    return row.key;
  };
  const shown = (value: unknown): string =>
    typeof value === 'string' ? value : JSON.stringify(value ?? '');

  async function load(): Promise<void> {
    try {
      const { data } = await readConfig({ throwOnError: true });
      items = data.items;
    } catch (error) {
      failed = true;
      if (isForbidden(error)) adminOnly = true;
      message = isForbidden(error) ? t('settings.admin_only') : t('settings.error');
    }
    loaded = true;
  }

  onMount(load);

  async function save(key: string, value: string): Promise<void> {
    failed = false;
    message = '';
    try {
      const { data } = await patchConfig({
        headers: headers(),
        body: { values: { [key]: value } },
        throwOnError: true
      });
      items = data.items;
      message = t('settings.saved');
    } catch (error) {
      if (needsReauth(error)) {
        retry = () => save(key, value);
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
        return;
      }
      failed = true;
      message =
        problemOf(error).code === 'config.key_readonly'
          ? t('settings.locked_refused')
          : t('settings.refused');
    }
  }

  function afterReauth(): void {
    const again = retry;
    retry = null;
    reauthMethods = null;
    if (again) void again();
  }

  function cancelReauth(): void {
    retry = null;
    reauthMethods = null;
  }

  type Entry = { href: string; label: string; icon: IconName };
  const MINE: Entry[] = [
    { href: '/reglages/notifications', label: t('settings.link.notifications'), icon: 'send' },
    { href: '/reglages/apparence', label: t('settings.link.appearance'), icon: 'sun' },
    { href: '/reglages/compte-kavita', label: t('kavita.title'), icon: 'plug' },
    { href: '/reglages/compte/oidc', label: t('oidc.title'), icon: 'key' }
  ];
  const ADMIN: { title: string; entries: Entry[] }[] = [
    {
      title: t('settings.group.library'),
      entries: [
        { href: '/reglages/profils', label: t('settings.link.profiles'), icon: 'users' },
        { href: '/reglages/etageres', label: t('shelves.title'), icon: 'books' },
        { href: '/reglages/destinations', label: t('destinations.title'), icon: 'send' },
        { href: '/reglages/adoption', label: t('settings.link.adoption'), icon: 'inbox' },
        { href: '/a-classer', label: t('settings.link.to_rate'), icon: 'stamp' },
        { href: '/corbeille', label: t('settings.link.trash'), icon: 'trash' }
      ]
    },
    {
      title: t('settings.group.instance'),
      entries: [
        { href: '/sante', label: t('settings.link.health'), icon: 'pulse' },
        { href: '/sante/stockage', label: t('settings.link.storage'), icon: 'drive' },
        { href: '/activite/file', label: t('settings.link.queue'), icon: 'tasks' },
        { href: '/activite/historique', label: t('settings.link.history'), icon: 'clock' },
        { href: '/sources', label: t('settings.link.sources'), icon: 'antenna' },
        { href: '/reglages/webhooks', label: t('webhooks.title'), icon: 'plug' }
      ]
    },
    {
      title: t('settings.group.devices'),
      entries: [
        { href: '/appareils', label: t('settings.link.devices'), icon: 'device' },
        { href: '/appareils/jetons', label: t('settings.link.device_tokens'), icon: 'key' }
      ]
    }
  ];

  const groups = $derived.by(() => {
    const grouped: Record<ConfigRow['level'], ConfigRow[]> = {
      essential: [],
      advanced: [],
      expert: []
    };
    for (const row of items) grouped[row.level].push(row);
    return grouped;
  });
</script>

<svelte:head>
  <title>{t('settings.title')}</title>
</svelte:head>

<main class="page">
  <PageHead title={t('settings.title')} lede={t('settings.lede')} />

  <nav aria-label={t('settings.mine')} class="hub">
    <h2 class="visually-hidden">{t('settings.mine')}</h2>
    {#if !adminOnly}
      {#each ADMIN as group (group.title)}
        <section class="hub__group">
          <h3>{group.title}</h3>
          <ul>
            {#each group.entries as entry (entry.href)}
              <li>
                <!-- eslint-disable svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
                {#if entry.href === '/a-classer'}
                  <a href="/a-classer"><Icon name={entry.icon} />{entry.label}</a>
                {:else if entry.href === '/corbeille'}
                  <a href="/corbeille"><Icon name={entry.icon} />{entry.label}</a>
                {:else if entry.href === '/appareils'}
                  <a href="/appareils"><Icon name={entry.icon} />{entry.label}</a>
                {:else}
                  <a href={entry.href}><Icon name={entry.icon} />{entry.label}</a>
                {/if}
                <!-- eslint-enable svelte/no-navigation-without-resolve -->
              </li>
            {/each}
          </ul>
        </section>
      {/each}
    {/if}
    <section class="hub__group">
      <h3>{t('settings.group.mine')}</h3>
      <ul>
        {#each MINE as entry (entry.href)}
          <li>
            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
            <a href={entry.href}><Icon name={entry.icon} />{entry.label}</a>
          </li>
        {/each}
      </ul>
    </section>
  </nav>

  {#if message}
    <Alert tone={failed ? 'danger' : 'success'} title={message} live={failed ? 'assertive' : 'polite'} />
  {/if}

  {#if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('settings.loading')}</p>
      <Skeleton /><Skeleton /><Skeleton short />
    </div>
  {:else if !adminOnly}
    {#each LEVELS as level (level)}
      <section class="section" data-level={level} aria-labelledby={`level-${level}`}>
        <div class="section-head"><h2 id={`level-${level}`}>{t(`settings.level.${level}`)}</h2></div>
        <SettingsForm label={t(`settings.level.${level}`)}>
          {#each groups[level] as row (row.key)}
            <div data-testid="setting-field" data-key={row.key}>
              <SettingRow label={labelOf(row)} meta={row.key} help={helpOf(row) ?? undefined}>
                {#snippet children(control)}
                  {#if row.secret}
                    <p class="origin"><Icon name="lock" size="sm" />{t('settings.secret')}</p>
                  {:else}
                    <Input
                      {...control}
                      mono
                      type="text"
                      value={shown(row.value)}
                      disabled={row.locked}
                      onchange={(e: Event) => save(row.key, (e.target as HTMLInputElement).value)}
                    />
                    {#if row.locked}
                      <p class="origin"><Icon name="lock" size="sm" />{originText(row)}</p>
                    {/if}
                  {/if}
                  {#if row.warning}
                    <p class="warn"><Icon name="alert" size="sm" />{row.warning}</p>
                  {/if}
                {/snippet}
              </SettingRow>
            </div>
          {/each}
        </SettingsForm>
      </section>
    {/each}
  {/if}

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}
</main>

<style>
  .hub {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 14rem), 1fr));
    gap: var(--space-5) var(--space-6);
    margin-bottom: var(--space-6);
  }
  .hub__group h3 {
    margin: 0 0 var(--space-2);
    padding-bottom: var(--space-1);
    border-bottom: var(--rule-hair) solid var(--color-ink);
    font-size: var(--step-1);
  }
  .hub ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .hub a {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-height: 2.75rem;
    color: var(--color-ink);
    text-decoration: none;
    overflow-wrap: anywhere;
  }
  .hub a:hover {
    color: var(--color-accent-text);
    text-decoration: underline;
  }
  .origin {
    display: flex;
    gap: var(--space-1);
    align-items: flex-start;
    margin: var(--space-1) 0 0;
    font-size: var(--step--1);
    color: var(--color-muted);
    overflow-wrap: anywhere;
  }
  .warn {
    display: flex;
    gap: var(--space-1);
    margin: var(--space-1) 0 0;
    color: var(--color-warn);
    font-size: var(--step--1);
  }
</style>
