<script lang="ts">
  // A modal dialog (bits-ui: focus trapped, Escape and the veil close it, the focus returns to
  // what opened it, the page behind does not scroll). A sheet laid over the page: the title, the
  // body, and a foot of actions (the confirming one last, on the right; stacked on a phone).
  // `open` is bindable; a trigger is the screen's own button that sets it.
  import type { Snippet } from 'svelte';
  import { Dialog } from 'bits-ui';
  import { t } from '#lib/i18n/index.ts';
  import Icon from '../atoms/Icon.svelte';

  let {
    open = $bindable(false),
    title,
    description,
    size = 'md',
    dismissible = true,
    children,
    footer,
    onclose
  }: {
    open?: boolean;
    title: string;
    description?: string;
    size?: 'md' | 'lg';
    /** false: Escape and the veil do not close it (an action is running) */
    dismissible?: boolean;
    children?: Snippet;
    footer?: Snippet;
    onclose?: () => void;
  } = $props();
</script>

<Dialog.Root
  bind:open
  onOpenChange={(next) => {
    if (!next) onclose?.();
  }}
>
  <Dialog.Portal>
    <Dialog.Overlay>
      {#snippet child({ props })}
        <div {...props} class="veil"></div>
      {/snippet}
    </Dialog.Overlay>
    <Dialog.Content
      onEscapeKeydown={(event) => {
        if (!dismissible) event.preventDefault();
      }}
      onInteractOutside={(event) => {
        if (!dismissible) event.preventDefault();
      }}
    >
      {#snippet child({ props })}
        <div {...props} class="dialog dialog--{size}">
          <div class="dialog__head">
            <Dialog.Title>
              {#snippet child({ props: titleProps })}
                <h2 {...titleProps} class="dialog__title">{title}</h2>
              {/snippet}
            </Dialog.Title>
            {#if dismissible}
              <Dialog.Close>
                {#snippet child({ props: closeProps })}
                  <button {...closeProps} class="dialog__close" type="button" aria-label={t('ui.dialog.close')} title={t('ui.dialog.close')}>
                    <Icon name="x" />
                  </button>
                {/snippet}
              </Dialog.Close>
            {/if}
          </div>
          <div class="dialog__body">
            {#if description}
              <Dialog.Description>
                {#snippet child({ props: descriptionProps })}
                  <p {...descriptionProps} class="dialog__description">{description}</p>
                {/snippet}
              </Dialog.Description>
            {/if}
            {@render children?.()}
          </div>
          {#if footer}<div class="dialog__foot">{@render footer()}</div>{/if}
        </div>
      {/snippet}
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>

<style>
  .veil {
    position: fixed;
    inset: 0;
    z-index: var(--z-overlay);
    background: color-mix(in oklab, var(--color-overlay), transparent 45%);
  }
  .dialog {
    position: fixed;
    z-index: var(--z-dialog);
    left: 50%;
    top: 50%;
    transform: translate(-50%, -50%);
    display: flex;
    flex-direction: column;
    width: min(32rem, calc(100% - 2 * var(--space-4)));
    max-height: calc(100dvh - 2 * var(--space-4));
    border: var(--rule-hair) solid var(--color-ink);
    border-radius: var(--radius-lg);
    background: var(--color-surface-float);
    color: var(--color-ink);
    box-shadow: var(--shadow-3);
  }
  .dialog--lg {
    width: min(44rem, calc(100% - 2 * var(--space-4)));
  }
  .dialog__head {
    display: flex;
    align-items: start;
    gap: var(--space-3);
    padding: var(--space-5) var(--space-5) var(--space-3);
  }
  .dialog__title {
    flex: 1;
    margin: 0;
    font-size: var(--step-2);
  }
  .dialog__close {
    display: inline-grid;
    place-items: center;
    flex: none;
    width: var(--target);
    height: var(--target);
    margin: calc(-1 * var(--space-2)) calc(-1 * var(--space-2)) 0 0;
    padding: 0;
    border: 0;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--color-muted);
    cursor: pointer;
  }
  .dialog__close:hover {
    background: var(--color-surface-2);
    color: var(--color-ink);
  }
  .dialog__body {
    display: grid;
    gap: var(--space-4);
    padding: 0 var(--space-5) var(--space-4);
    overflow-y: auto;
  }
  .dialog__description {
    margin: 0;
    color: var(--color-muted);
  }
  .dialog__foot {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--space-3);
    padding: var(--space-4) var(--space-5);
    border-top: var(--rule-hair) solid var(--color-rule);
    background: var(--color-bg);
    border-radius: 0 0 var(--radius-lg) var(--radius-lg);
  }
  @media (max-width: 40rem) {
    .dialog__foot {
      flex-direction: column-reverse;
    }
    .dialog__foot > :global(*) {
      width: 100%;
    }
  }
</style>
