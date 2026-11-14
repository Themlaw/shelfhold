<script lang="ts">
  // A QR code drawn here, from the text it is given, by a local generator: the page makes no
  // request to draw it (EF-UX-004). The text is an address of Kavita and nothing else; the caller
  // gives it only a web address. The code stays dark on light whatever the theme, so a camera
  // reads it.
  import qrcode from 'qrcode-generator';

  let { text, label }: { text: string; label: string } = $props();

  const QUIET = 4; // the quiet zone, in modules, that the standard asks for
  const drawn = $derived.by(() => {
    const code = qrcode(0, 'M'); // type 0: the smallest version that holds the text
    code.addData(text);
    code.make();
    const size = code.getModuleCount();
    let path = '';
    for (let row = 0; row < size; row += 1) {
      for (let col = 0; col < size; col += 1) {
        if (code.isDark(row, col)) path += `M${col + QUIET} ${row + QUIET}h1v1h-1z`;
      }
    }
    return { size: size + 2 * QUIET, path };
  });
</script>

<svg
  class="qr"
  role="img"
  aria-label={label}
  data-qr-text={text}
  viewBox={`0 0 ${drawn.size} ${drawn.size}`}
  shape-rendering="crispEdges"
>
  <rect width={drawn.size} height={drawn.size} class="paper" />
  <path d={drawn.path} class="ink" />
</svg>

<style>
  .qr {
    width: 12rem;
    max-width: 100%;
    height: auto;
  }
  .paper {
    fill: Canvas;
  }
  .ink {
    fill: CanvasText;
  }
</style>
