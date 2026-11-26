<script lang="ts">
  // A cover is an object, not a thumbnail (the signature of « Papier et encre »). With an image,
  // the image, cut to 2:3. Without one, or when it fails to load, a cover composed in type from
  // the title: never a grey square. Five families: bound novel (three compositions and the
  // « collection blanche »), manga, webtoon, comic album. Below 6.5 rem wide the cover is only a
  // picture (the title is printed next to it); below 9 rem the author leaves it.
  // Decorative by default: the card that holds it says the title. `label` makes it an image.
  // A locked work keeps its place, without title or author.
  import { t } from '#lib/i18n/index.ts';
  import Icon from './Icon.svelte';
  import { MOTIFS } from './icons.ts';
  import ProgressRibbon from './ProgressRibbon.svelte';
  import Shapes from './Shapes.svelte';
  import CoverArt from './CoverArt.svelte';
  import { coverPlan, type CoverKind } from './cover.ts';

  let {
    title,
    seed = null,
    author = null,
    src = null,
    kind = 'book',
    number = null,
    size = 'fill',
    progress = null,
    done = false,
    locked = false,
    label
  }: {
    title: string;
    /** the identifier of the series: it picks the composition (the title when absent) */
    seed?: string | null;
    author?: string | null;
    src?: string | null;
    kind?: CoverKind;
    number?: string | number | null;
    size?: 'sm' | 'md' | 'lg' | 'fill';
    progress?: number | null;
    done?: boolean;
    locked?: boolean;
    label?: string;
  } = $props();

  let failed = $state(false);
  let loaded = $state(false);
  // a new address is a new chance for the image
  $effect(() => {
    void src;
    failed = false;
    loaded = false;
  });
  // an image already in the cache may have loaded before the handler was attached
  function settle(node: HTMLImageElement): void {
    if (node.complete && node.naturalWidth > 0) loaded = true;
  }

  const plan = $derived(coverPlan(seed ?? title, kind));
  const image = $derived(!locked && !!src && !failed);
  // the composed cover is always there, under the picture: while the image loads, scrolls into
  // view or fails, the slot is a cover and never an empty box
  const generated = $derived(locked ? 'locked' : plan.family === 'book' && plan.layout === 'blanche' ? 'blanche' : plan.family);
  const ribbon = $derived(!locked && (done || (progress !== null && progress > 0)));
</script>

<div
  class="cover cover--{size} cover--{generated} paper-{plan.paper} ink-{plan.ink} layout-{plan.layout} scene-{plan.scene?.name ?? 'none'}"
  role={label ? 'img' : undefined}
  aria-label={label}
  aria-hidden={label ? undefined : 'true'}
  data-family={generated}
  data-image={image ? (loaded ? 'loaded' : 'pending') : undefined}
>
  {#if generated === 'locked'}
    <span class="cover__frame"></span>
    <span class="cover__lock"><Icon name="lock" size="lg" /></span>
    <span class="cover__title">{t('ui.cover.locked')}</span>
  {:else if generated === 'manga' && plan.scene}
    <svg class="cover__art" viewBox={plan.scene.viewBox} preserveAspectRatio="xMidYMid slice" aria-hidden="true"><CoverArt nodes={plan.scene.nodes} /></svg>
    {#if author}<span class="cover__author">{author}</span>{/if}
    <span class="cover__title">{title}</span>
    {#if number !== null}<span class="cover__tome">{number}</span>{/if}
  {:else if generated === 'webtoon' && plan.scene}
    <svg class="cover__art" viewBox={plan.scene.viewBox} preserveAspectRatio="xMidYMid slice" aria-hidden="true"><CoverArt nodes={plan.scene.nodes} /></svg>
    {#if number !== null}<span class="cover__tag">{t('ui.cover.episode', { number: String(number) })}</span>{/if}
    <span class="cover__title">{title}</span>
    {#if author}<span class="cover__author">{author}</span>{/if}
  {:else if generated === 'comic' && plan.scene}
    {#if author}<span class="cover__author">{author}</span>{/if}
    <span class="cover__title">{title}</span>
    <span class="cover__panel">
      <svg class="cover__art" viewBox={plan.scene.viewBox} preserveAspectRatio="xMidYMid slice" aria-hidden="true"><CoverArt nodes={plan.scene.nodes} /></svg>
    </span>
  {:else if generated === 'blanche'}
    {#if author}<span class="cover__author">{author}</span>{/if}
    <span class="cover__title">{title}</span>
    <span class="cover__foot">
      <svg class="cover__motif" viewBox="0 0 40 40" aria-hidden="true"><Shapes shapes={MOTIFS.fleuron} /></svg>
      {#if number !== null}<span class="cover__tome">{number}</span>{/if}
    </span>
  {:else}
    <span class="cover__frame"></span>
    <span class="cover__author">{author ?? t('ui.cover.unknown_author')}</span>
    <span class="cover__title">{title}</span>
    <span class="cover__foot">
      <svg class="cover__motif" viewBox="0 0 40 40" aria-hidden="true"><Shapes shapes={MOTIFS[plan.motif]} /></svg>
      {#if number !== null}<span class="cover__tome">{number}</span>{/if}
    </span>
  {/if}
  {#if image}
    <img
      class="cover__img"
      class:is-loaded={loaded}
      {src}
      alt=""
      loading="lazy"
      decoding="async"
      use:settle
      onload={() => (loaded = true)}
      onerror={() => (failed = true)}
    />
  {/if}
  {#if ribbon}<ProgressRibbon value={progress ?? 1} {done} side={generated === 'manga' && !image ? 'left' : 'right'} />{/if}
</div>

<style>
  .cover {
    --cv: var(--cover-1);
    --cvi: var(--cover-ink-1);
    position: relative;
    display: grid;
    container: cover / inline-size;
    aspect-ratio: 2 / 3;
    width: 100%;
    overflow: hidden;
    background: var(--cv);
    color: var(--cvi);
    border-radius: var(--radius-cover) var(--radius-xs) var(--radius-xs) var(--radius-cover);
    box-shadow: var(--shadow-cover);
    font-family: var(--font-text);
  }
  .cover > * {
    position: relative;
    min-width: 0;
  }
  .cover--sm {
    width: 3rem;
    flex: none;
  }
  .cover--md {
    width: 7.5rem;
    flex: none;
  }
  .cover--lg {
    max-width: 17rem;
  }
  .paper-1 { --cv: var(--cover-1); --cvi: var(--cover-ink-1); }
  .paper-2 { --cv: var(--cover-2); --cvi: var(--cover-ink-2); }
  .paper-3 { --cv: var(--cover-3); --cvi: var(--cover-ink-3); }
  .paper-4 { --cv: var(--cover-4); --cvi: var(--cover-ink-4); }
  .paper-5 { --cv: var(--cover-5); --cvi: var(--cover-ink-5); }
  .paper-6 { --cv: var(--cover-6); --cvi: var(--cover-ink-6); }
  .paper-7 { --cv: var(--cover-7); --cvi: var(--cover-ink-7); }
  .paper-8 { --cv: var(--cover-8); --cvi: var(--cover-ink-8); }
  .ink-red { --bl: var(--cv-red); }
  .ink-blue { --bl: var(--cv-blue); }
  .ink-green { --bl: var(--cv-green); }
  .ink-vermilion { --bl: var(--cv-vermilion); }
  .ink-teal { --bl: var(--cv-teal); }
  .ink-plum { --bl: var(--cv-plum); }

  .cover__img {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    z-index: 1;
    opacity: 0;
    transition: opacity var(--dur-2) var(--ease-out);
  }
  .cover__img.is-loaded {
    opacity: 1;
  }
  .cover__title {
    overflow-wrap: break-word;
    hyphens: manual;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .cover__frame {
    position: absolute;
    inset: 5% 5% 5% 12%;
    border: var(--rule-hair) solid currentColor;
    opacity: 0.55;
    pointer-events: none;
  }
  .cover__motif {
    fill: none;
    stroke: currentColor;
    stroke-width: 1.4;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .cover__tome {
    font-weight: 600;
    font-variant-numeric: oldstyle-nums;
  }

  /* -- Bound novel: cloth, hinge, ruled frame, engraved motif ----------------- */
  .cover--book {
    grid-template-rows: auto 1fr auto;
  }
  .cover--book::before {
    content: '';
    position: absolute;
    inset: 0 auto 0 0;
    width: 7%;
    background: color-mix(in oklab, var(--cv), black 22%);
  }
  .cover--book::after {
    content: '';
    position: absolute;
    inset: 0 auto 0 8.5%;
    width: var(--rule-hair);
    background: color-mix(in oklab, var(--cv), white 18%);
  }
  .cover--book .cover__author {
    grid-row: 1;
    margin: 12cqw 9cqw 0 15cqw;
    font-size: 7.5cqw;
    font-style: italic;
    line-height: 1.2;
  }
  .cover--book .cover__title {
    grid-row: 2;
    align-self: center;
    margin: 0 10cqw 0 15cqw;
    font-size: 13cqw;
    font-weight: 600;
    line-height: 1.05;
    letter-spacing: -0.01em;
    text-wrap: balance;
    -webkit-line-clamp: 5;
    line-clamp: 5;
  }
  .cover__foot {
    grid-row: 3;
    margin: 0 9cqw 9cqw 15cqw;
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 4cqw;
  }
  .cover--book .cover__motif {
    width: 30cqw;
    height: 30cqw;
  }
  .cover--book .cover__tome {
    font-size: 9cqw;
  }
  /* b: a light leather title piece, a large gilt motif */
  .cover--book.layout-b {
    grid-template-rows: auto auto 1fr;
  }
  .layout-b .cover__frame {
    border-width: var(--rule-mid);
    border-style: double;
    opacity: 0.7;
    inset: 4% 4% 4% 11%;
  }
  .cover--book.layout-b .cover__title {
    grid-row: 1;
    align-self: start;
    margin: 14cqw 10cqw 0 16cqw;
    padding: 4cqw 4cqw 5cqw;
    text-align: center;
    background: var(--cvi);
    color: var(--cv);
    font-size: 12cqw;
    -webkit-line-clamp: 4;
    line-clamp: 4;
  }
  .cover--book.layout-b .cover__author {
    grid-row: 2;
    margin: 3cqw 10cqw 0 16cqw;
    text-align: center;
  }
  .cover--book.layout-b .cover__foot {
    align-self: center;
    justify-content: center;
    margin: 0 8cqw 6cqw 15cqw;
  }
  .cover--book.layout-b .cover__motif {
    width: 44cqw;
    height: 44cqw;
    stroke-width: 1.1;
  }
  /* c: an oval medallion, centred italic title */
  .layout-c .cover__frame {
    inset: 14% 12% 18% 19%;
    border-radius: 50%;
    border-width: var(--rule-mid);
    opacity: 0.8;
  }
  .cover--book.layout-c .cover__author {
    margin: 8cqw 6cqw 0 12cqw;
    text-align: center;
    font-size: 7cqw;
  }
  .cover--book.layout-c .cover__title {
    margin: 0 16cqw 0 22cqw;
    text-align: center;
    font-style: italic;
    font-weight: 700;
    font-size: 12.5cqw;
    -webkit-line-clamp: 4;
    line-clamp: 4;
  }
  .cover--book.layout-c .cover__foot {
    justify-content: center;
    margin: 0 6cqw 6cqw 12cqw;
  }
  .cover--book.layout-c .cover__motif {
    width: 18cqw;
    height: 18cqw;
  }

  /* -- « Collection blanche »: paper, double rule, coloured italic title ------ */
  .cover--blanche {
    --cv: var(--cv-blanche);
    --cvi: var(--cv-ink);
    grid-template-rows: auto auto 1fr auto;
    box-shadow:
      var(--shadow-cover),
      inset 0 0 0 var(--rule-hair) color-mix(in oklab, var(--cv), black 14%);
  }
  .cover--blanche::before {
    content: '';
    position: absolute;
    left: 10%;
    right: 10%;
    top: 4.6%;
    height: 2%;
    border-top: 1.2cqw solid var(--bl);
    border-bottom: 0.5cqw solid var(--bl);
  }
  .cover--blanche .cover__author {
    grid-row: 1;
    margin: 14cqw 10cqw 0;
    font: 500 7cqw / 1.2 var(--font-ui);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .cover--blanche .cover__title {
    grid-row: 2;
    margin: 4cqw 10cqw 0;
    color: var(--bl);
    font: italic 600 15cqw / 1 var(--font-text);
    letter-spacing: -0.01em;
    text-wrap: balance;
    -webkit-line-clamp: 5;
    line-clamp: 5;
  }
  .cover--blanche .cover__foot {
    grid-row: 4;
    margin: 0 10cqw 9cqw;
    display: grid;
    justify-items: center;
    gap: 2cqw;
    color: var(--bl);
  }
  .cover--blanche .cover__motif {
    width: 18cqw;
    height: 18cqw;
  }
  .cover--blanche .cover__tome {
    font-size: 8cqw;
    color: var(--cv-ink);
  }

  /* -- The drawn scenes: ink, paper and the scene's own colours (cover-art.ts) - */
  .cover__art {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
  }
  .cover__art :global(.k) {
    fill: var(--cv-ink);
  }
  .cover__art :global(.p) {
    fill: var(--cv-light);
  }
  .cover__art :global(.a) {
    fill: var(--c-a);
  }
  .cover__art :global(.b) {
    fill: var(--c-b);
  }
  .cover__art :global(.c) {
    fill: var(--c-c);
  }
  .cover__art :global(.d) {
    fill: var(--c-d, var(--cv-brick));
  }
  .cover__art :global(:is(.ks, .kf, .ps)) {
    fill: none;
    stroke: var(--cv-ink);
    stroke-width: 3;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .cover__art :global(.kf) {
    stroke-width: 1.4;
  }
  .cover__art :global(.ks--thick) {
    stroke-width: 6;
  }
  .cover__art :global(.ps) {
    stroke: var(--cv-light);
    stroke-width: 2.4;
  }
  .cover__art :global(.ps--soft) {
    opacity: 0.6;
  }
  .cover__art :global(.sk) {
    stroke: var(--cv-ink);
    stroke-width: 2.4;
    stroke-linejoin: round;
  }
  .cover__art :global(.beam) {
    opacity: 0.5;
  }
  .scene-hokusai { --c-a: var(--cv-vermilion); }
  .scene-lucioles { --c-a: var(--cv-lime); --c-on: var(--cv-ink); }
  .scene-heron { --c-a: var(--cv-teal); }
  .scene-abysses { --c-a: var(--cv-cobalt); }
  .scene-phare { --c-a: var(--cv-deep); --c-b: var(--cv-sea); --c-c: var(--cv-amber); --c-strip: var(--cv-amber); --c-strip-ink: var(--cv-ink); }
  .scene-marees { --c-a: var(--cv-coral); --c-b: var(--cv-plum); --c-c: var(--cv-sand); --c-strip: var(--cv-plum); --c-strip-ink: var(--cv-sand); }
  .scene-nemo { --c-a: var(--cv-night); --c-b: var(--cv-red); --c-c: var(--cv-sun); --c-t: var(--cv-red); }
  .scene-vieuxbois { --c-a: var(--cv-sky); --c-b: var(--cv-green); --c-c: var(--cv-ochre); --c-t: var(--cv-green); }
  .scene-moritz { --c-a: var(--cv-sun); --c-b: var(--cv-brick); --c-c: var(--cv-ochre); --c-t: var(--cv-blue); }
  .scene-lievre { --c-a: var(--cv-plum); --c-c: var(--cv-sand); --c-t: var(--cv-plum); }

  /* -- Manga: black ink on paper, one colour, vertical title band, big number - */
  .cover--manga {
    --cv: var(--cv-paper);
    --cvi: var(--cv-ink);
    grid-template: 'author band' auto '. band' 1fr 'tome band' auto / minmax(0, 1fr) auto;
  }
  .cover--manga .cover__title {
    grid-area: band;
    writing-mode: vertical-rl;
    max-height: 100%;
    padding: 6cqw 3.5cqw;
    background: var(--c-a);
    color: var(--c-on, var(--cv-paper));
    font: 700 12.5cqw / 1.05 var(--font-ui);
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }
  .cover--manga .cover__author {
    grid-area: author;
    justify-self: start;
    margin: 5cqw 0 0 5cqw;
    padding: 1cqw 2cqw;
    background: var(--cv-paper);
    font: 500 6.5cqw / 1.2 var(--font-ui);
  }
  .cover--manga .cover__tome {
    grid-area: tome;
    justify-self: start;
    margin: 0 0 3cqw 5cqw;
    padding: 0 2.5cqw;
    background: var(--cv-paper);
    color: var(--cv-ink);
    font: italic 900 24cqw / 1 var(--font-text);
    font-variant-numeric: lining-nums;
  }

  /* -- Webtoon: full colour, the title in a band at the bottom --------------- */
  .cover--webtoon {
    --cv: var(--c-a);
    grid-template-rows: auto 1fr auto auto;
  }
  .cover--webtoon .cover__tag {
    grid-row: 1;
    justify-self: start;
    margin: 5cqw;
    padding: 1cqw 3cqw;
    background: var(--cv-ink);
    color: var(--cv-light);
    font: 700 7cqw / 1.3 var(--font-ui);
    border-radius: var(--radius-xs);
  }
  .cover--webtoon .cover__title {
    grid-row: 3;
    padding: 5cqw 7cqw 3cqw;
    background: var(--c-strip);
    color: var(--c-strip-ink);
    font: 700 14cqw / 0.98 var(--font-ui);
    letter-spacing: -0.02em;
    word-spacing: 0.12em;
    text-wrap: balance;
    -webkit-line-clamp: 3;
    line-clamp: 3;
  }
  .cover--webtoon .cover__author {
    grid-row: 4;
    padding: 0 7cqw 6cqw;
    background: var(--c-strip);
    color: var(--c-strip-ink);
    font: italic 7cqw / 1.2 var(--font-text);
  }
  @container cover (width < 9rem) {
    .cover--blanche .cover__title {
      margin-top: 16cqw;
    }
  }
  .cover--webtoon .cover__title:last-child {
    padding-bottom: 7cqw;
  }

  /* -- Comic album: a title band in heavy type, an inked « clear line » panel -- */
  .cover--comic {
    --cv: var(--cv-cream);
    --cvi: var(--cv-ink);
    grid-template-rows: auto auto minmax(0, 1fr);
  }
  .cover--comic .cover__author {
    grid-row: 1;
    margin: 6cqw 7cqw 0;
    font: 700 6.5cqw / 1.2 var(--font-ui);
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  .cover--comic .cover__title {
    grid-row: 2;
    margin: 6cqw 7cqw 4cqw;
    color: var(--c-t);
    font: 800 15cqw / 0.92 var(--font-text);
    letter-spacing: -0.02em;
    text-wrap: balance;
    -webkit-line-clamp: 3;
    line-clamp: 3;
  }
  .cover--comic .cover__author + .cover__title {
    margin-top: 1cqw;
  }
  .cover__panel {
    grid-row: 3;
    margin: 0 6cqw 6cqw;
    border: 1.2cqw solid var(--cv-ink);
    min-height: 0;
    overflow: hidden;
  }
  .cover__panel .cover__art {
    position: static;
  }
  @container cover (width < 6.5rem) {
    .cover--comic .cover__panel {
      grid-row: 1 / -1;
      margin: 6cqw;
    }
  }

  /* -- Locked: the place stays, the content does not --------------------------- */
  .cover--locked {
    --cv: var(--color-surface-2);
    --cvi: var(--color-muted);
    border: var(--rule-hair) solid var(--color-rule-strong);
    grid-template-rows: 1fr auto 1fr;
    justify-items: center;
  }
  .cover--locked .cover__frame {
    border-style: dashed;
    inset: 5%;
  }
  .cover--locked .cover__lock {
    grid-row: 1;
    align-self: end;
  }
  .cover--locked .cover__title {
    grid-row: 2;
    margin: 4cqw 10cqw;
    text-align: center;
    font-style: italic;
    font-size: 10cqw;
    -webkit-line-clamp: 3;
    line-clamp: 3;
  }

  /* The rule of sizes: below 6.5 rem only a picture, below 9 rem the author leaves */
  @container cover (width < 6.5rem) {
    .cover__title,
    .cover__author,
    .cover__tag,
    .cover__tome {
      display: none !important;
    }
  }
  @container cover (width < 9rem) {
    .cover__author {
      display: none !important;
    }
  }
  /* the « collection blanche » has no picture: its title is the cover, so it stays at any size */
  @container cover (width < 6.5rem) {
    .cover--blanche:not([data-image]) .cover__title {
      display: -webkit-box !important;
      margin-inline: 8cqw;
      font-size: 16cqw;
    }
  }
</style>
