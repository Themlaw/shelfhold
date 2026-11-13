<script lang="ts">
  // EF-UX-008: an empty state is never a dead end: it names what is missing and offers the
  // action that fills it.
  let p = $props();
  const title = p.title ?? p.heading ?? p.label ?? '';
  const description = p.description ?? p.text ?? p.message ?? '';
  const actionLabel = p.actionLabel ?? p.action ?? p.actionText ?? p.buttonLabel ?? p.buttonText ?? '';
  const href = p.href ?? p.to ?? p.url ?? '';
  const onAction = p.onAction ?? p.onClick ?? p.actionHandler;
</script>

<section class="empty-state" aria-label={title}>
  <h2 class="title">{title}</h2>
  {#if description}
    <p class="description">{description}</p>
  {/if}
  {#if actionLabel}
    {#if href}
      <a class="action" href={href}>{actionLabel}</a>
    {:else}
      <button class="action" type="button" onclick={() => onAction?.()}>{actionLabel}</button>
    {/if}
  {/if}
</section>

<style>
  .empty-state {
    text-align: center;
    padding: 2rem 1rem;
    border: 1px dashed var(--color-rule, #ccc);
    border-radius: 0.5rem;
  }
  .title {
    margin: 0 0 0.5rem;
    font-size: 1.125rem;
  }
  .description {
    color: var(--color-muted, #666);
    margin: 0 0 1rem;
  }
  .action {
    display: inline-block;
    padding: 0.5rem 1rem;
    border-radius: 0.375rem;
    background: var(--color-accent, #333);
    color: #fff;
    text-decoration: none;
    border: none;
    cursor: pointer;
  }
</style>