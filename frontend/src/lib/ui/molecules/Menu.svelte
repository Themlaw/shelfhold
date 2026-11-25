<script lang="ts" module>
  import type { IconName } from '../atoms/icons.ts';

  export type MenuItem =
    | { kind?: 'item'; label: string; icon?: IconName; href?: string; danger?: boolean; onselect?: () => void }
    | { kind: 'separator' };
  export type MenuChoice = { value: string; label: string; icon?: IconName };
</script>

<script lang="ts">
  // A menu that opens from a button (bits-ui: arrows, Home and End, typeahead, Escape returns the
  // focus to the button). Items are links or actions; `choices` is one exclusive choice (radio
  // items, `value` bindable), printed above the items. The row actions of a register, the
  // account menu of the header.
  import type { Snippet } from 'svelte';
  import { DropdownMenu } from 'bits-ui';
  import { goto } from '$app/navigation';
  import Icon from '../atoms/Icon.svelte';

  let {
    items = [],
    choices,
    value = $bindable(),
    choicesLabel,
    align = 'end',
    onchoose,
    trigger
  }: {
    items?: readonly MenuItem[];
    choices?: readonly MenuChoice[];
    value?: string;
    choicesLabel?: string;
    align?: 'start' | 'end';
    onchoose?: (value: string) => void;
    /** the button: spread `props` on a <button> */
    trigger: Snippet<[{ props: Record<string, unknown>; open: boolean }]>;
  } = $props();

  let open = $state(false);
</script>

<DropdownMenu.Root bind:open>
  <DropdownMenu.Trigger>
    {#snippet child({ props })}
      {@render trigger({ props, open })}
    {/snippet}
  </DropdownMenu.Trigger>
  <DropdownMenu.Portal>
    <DropdownMenu.Content {align} sideOffset={6}>
      {#snippet child({ wrapperProps, props, open: shown })}
        {#if shown}
          <div {...wrapperProps} class="menu-layer">
            <div {...props} class="menu">
              {#if choices}
                <DropdownMenu.RadioGroup
                  bind:value
                  onValueChange={(next) => onchoose?.(next)}
                >
                  {#snippet child({ props: groupProps })}
                    <div {...groupProps} aria-label={choicesLabel}>
                      {#each choices as choice (choice.value)}
                        <DropdownMenu.RadioItem value={choice.value}>
                          {#snippet child({ props: itemProps, checked })}
                            <div {...itemProps} class="menu__item">
                              {#if choice.icon}<Icon name={choice.icon} />{/if}
                              <span class="menu__label">{choice.label}</span>
                              {#if checked}<Icon name="check" size="sm" />{/if}
                            </div>
                          {/snippet}
                        </DropdownMenu.RadioItem>
                      {/each}
                    </div>
                  {/snippet}
                </DropdownMenu.RadioGroup>
                {#if items.length > 0}<div class="menu__sep" role="separator"></div>{/if}
              {/if}
              {#each items as item, index (index)}
                {#if item.kind === 'separator'}
                  <DropdownMenu.Separator>
                    {#snippet child({ props: sepProps })}<div {...sepProps} class="menu__sep"></div>{/snippet}
                  </DropdownMenu.Separator>
                {:else}
                  <DropdownMenu.Item
                    onSelect={() => {
                      if (item.onselect) item.onselect();
                      // eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin
                      else if (item.href) void goto(item.href);
                    }}
                  >
                    {#snippet child({ props: itemProps })}
                      <div {...itemProps} class="menu__item" class:menu__item--danger={item.danger}>
                        {#if item.icon}<Icon name={item.icon} />{/if}
                        <span class="menu__label">{item.label}</span>
                      </div>
                    {/snippet}
                  </DropdownMenu.Item>
                {/if}
              {/each}
            </div>
          </div>
        {/if}
      {/snippet}
    </DropdownMenu.Content>
  </DropdownMenu.Portal>
</DropdownMenu.Root>

<style>
  .menu-layer {
    z-index: var(--z-dropdown);
  }
  .menu {
    min-width: 14rem;
    padding: var(--space-1);
    background: var(--color-surface-float);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-2);
  }
  .menu__item {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    min-height: var(--target);
    padding: 0 var(--space-3);
    border-radius: var(--radius-sm);
    color: var(--color-ink);
    cursor: pointer;
    user-select: none;
  }
  .menu__item:global([data-highlighted]) {
    background: var(--color-surface-2);
    outline: 2px solid var(--color-focus);
    outline-offset: -2px;
  }
  .menu__item > :global(.icon) {
    color: var(--color-muted);
  }
  .menu__label {
    flex: 1;
  }
  .menu__item--danger,
  .menu__item--danger > :global(.icon) {
    color: var(--color-err);
  }
  .menu__sep {
    height: var(--rule-hair);
    margin: var(--space-1) 0;
    background: var(--color-rule);
  }
</style>
