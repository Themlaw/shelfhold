<script lang="ts">
  // How far a reading or a task went: a rule that fills (role="progressbar"). `label` names it
  // for assistive technology (« Lecture de Vingt mille lieues »); the percentage is said by it.
  let {
    value,
    label,
    tone = 'accent',
    thick = false
  }: { value: number; label: string; tone?: 'accent' | 'warning' | 'danger' | 'done'; thick?: boolean } =
    $props();

  const percent = $derived(Math.round(Math.min(1, Math.max(0, Number.isFinite(value) ? value : 0)) * 100));
</script>

<span
  class="progress progress--{tone}"
  class:progress--thick={thick}
  role="progressbar"
  aria-label={label}
  aria-valuemin={0}
  aria-valuemax={100}
  aria-valuenow={percent}
>
  <span class="progress__bar" style:inline-size="{percent}%"></span>
</span>

<style>
  .progress {
    position: relative;
    display: block;
    height: var(--rule-thick);
    background: var(--color-rule);
    border-radius: var(--radius-pill);
    overflow: hidden;
  }
  .progress--thick {
    height: calc(var(--rule-thick) + var(--rule-mid));
  }
  .progress__bar {
    position: absolute;
    inset: 0 auto 0 0;
    background: var(--color-accent);
  }
  .progress--warning .progress__bar {
    background: var(--color-warn);
  }
  .progress--danger .progress__bar {
    background: var(--color-err);
  }
  .progress--done .progress__bar {
    background: var(--color-ok);
  }
</style>
