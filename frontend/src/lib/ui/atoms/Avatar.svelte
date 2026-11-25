<script lang="ts" module>
  /** The initials of a name: the first letter of its first two words, upper case. */
  export function initials(name: string): string {
    const words = name.trim().split(/[\s._-]+/).filter(Boolean);
    const letters = words.slice(0, 2).map((w) => [...w][0] ?? '');
    return letters.join('').toLocaleUpperCase() || '?';
  }
</script>

<script lang="ts">
  // The ex-libris of a profile: initials in a double-ruled roundel. A child's has a dashed rule,
  // an administrator's a blue ground. Decorative when the name is written next to it.
  let {
    name,
    kind = 'adult',
    size = 'md',
    label
  }: { name: string; kind?: 'adult' | 'child' | 'admin'; size?: 'sm' | 'md' | 'lg'; label?: string } =
    $props();
</script>

<span
  class="avatar avatar--{size} avatar--{kind}"
  role={label ? 'img' : undefined}
  aria-label={label}
  aria-hidden={label ? undefined : 'true'}>{initials(name)}</span
>

<style>
  .avatar {
    display: inline-grid;
    place-items: center;
    flex: none;
    width: 2.5rem;
    height: 2.5rem;
    font: italic 600 var(--step-0) / 1 var(--font-text);
    color: var(--color-ink);
    background: var(--color-surface);
    border: var(--rule-mid) solid var(--color-ink);
    border-radius: var(--radius-pill);
    outline: var(--rule-hair) solid var(--color-ink);
    outline-offset: 2px;
  }
  .avatar--sm {
    width: 2rem;
    height: 2rem;
    font-size: var(--step--1);
    outline-offset: 1px;
  }
  .avatar--lg {
    width: 4rem;
    height: 4rem;
    font-size: var(--step-2);
    outline-offset: 3px;
  }
  .avatar--child {
    border-style: dashed;
  }
  .avatar--admin {
    background: var(--color-accent-soft);
  }
</style>
