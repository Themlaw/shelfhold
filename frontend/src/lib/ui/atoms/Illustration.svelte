<script lang="ts" module>
  export type IllustrationName = 'missing' | 'locked' | 'fallen' | 'offline' | 'empty' | 'nothing-found';
</script>

<script lang="ts">
  // The shelf drawn in a line (the tablet, the signature of the direction), told in six states:
  // the missing volume (404), the volume shelved higher with its padlock (403), the fallen
  // volume (500), the shelf behind a cut line (offline), the empty shelf between its book-ends,
  // and the magnifier over a row (no result). Always decorative: the page says it in words.
  let { name }: { name: IllustrationName } = $props();
</script>

<svg class="art art--{name}" viewBox="0 0 220 170" aria-hidden="true" focusable="false">
  <path d="M6 140h208M6 146h208" />
  {#if name === 'missing'}
    <rect class="wash" x="96" y="44" width="26" height="96" />
    <rect x="96" y="44" width="26" height="96" stroke-dasharray="4 4" />
    <path d="M22 140V56h22v84M26 66h14M26 128h14" />
    <path d="M46 140V40h20v100M50 52h12" />
    <path d="M68 140V64h24v76M72 76h16" />
    <path d="M126 140V50h22v90M130 62h14M130 128h14" />
    <path d="m150 140 16-88 20 4-16 84" />
    <path d="M190 140V72h18v68" />
    <path class="accent" d="M104 30c0-6 10-6 10 0 0 4-5 5-5 9M109 44v.1" />
  {:else if name === 'locked'}
    <path d="M30 140V60h22v80M34 72h14" />
    <path d="M54 140V48h20v92M58 60h12" />
    <rect class="wash" x="84" y="34" width="56" height="106" />
    <rect x="84" y="34" width="56" height="106" />
    <path d="M90 34v106" />
    <rect class="accent" x="100" y="86" width="26" height="20" rx="2" />
    <path class="accent" d="M105 86v-6a8 8 0 0 1 16 0v6" />
    <path d="M150 140V56h22v84M154 68h14" />
    <path d="m174 140 14-80 18 3-14 77" />
  {:else if name === 'fallen'}
    <path d="M22 140V56h22v84M26 66h14" />
    <path d="M46 140V40h20v100M50 52h12" />
    <path d="m70 140 30-74 18 8-27 66" />
    <rect class="wash" x="120" y="118" width="70" height="22" />
    <rect x="120" y="118" width="70" height="22" />
    <path d="M128 118v22M182 118v22" />
    <path class="accent" d="M150 96l6-10 6 10M156 100v.1M156 74v8" />
    <path d="M196 140V70h16v70" />
  {:else if name === 'offline'}
    <path d="M24 140V60h22v80M28 72h14" />
    <path d="M48 140V46h20v94M52 58h12" />
    <path d="M70 140V66h24v74M74 78h16" />
    <path d="M150 140V56h22v84M154 68h14" />
    <path d="m174 140 14-80 18 3-14 77" />
    <path class="accent" d="M104 70a20 20 0 0 1 36 4h2a12 12 0 0 1 4 23M122 104h-14a14 14 0 0 1-6-27" />
    <path class="accent" d="m100 56 48 56" />
  {:else if name === 'empty'}
    <path d="M30 140V96h10l18 44" />
    <path d="M190 140V96h-10l-18 44" />
    <path class="dash" d="M70 140V80h22v60M100 140V70h20v70M128 140V86h22v54" stroke-dasharray="3 5" />
  {:else}
    <path d="M22 140V56h22v84M26 66h14" />
    <path d="M46 140V40h20v100M50 52h12" />
    <path d="M68 140V64h24v76M72 76h16" />
    <path d="M126 140V50h22v90M130 62h14" />
    <path d="M190 140V72h18v68" />
    <circle class="accent" cx="120" cy="70" r="20" />
    <path class="accent" d="m134 84 22 22" />
  {/if}
</svg>

<style>
  .art {
    display: block;
    width: 100%;
    height: auto;
    color: var(--color-ink);
    fill: none;
    stroke: currentColor;
    stroke-width: 1.1;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .wash {
    fill: var(--color-surface-2);
    stroke: none;
  }
  .accent {
    stroke: var(--color-accent-text);
    stroke-width: 1.4;
  }
  .dash {
    stroke: var(--color-subtle);
  }
</style>
