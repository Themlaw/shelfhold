<script lang="ts">
  import { t } from '#lib/i18n.ts';
  let { children } = $props();
</script>
<nav aria-label="Principal" style="background:#fffdf8;padding:8px">
  <a href="/" style="color:#7a3515">{t('nav_home')}</a> ·
  <a href="/bibliotheque" style="color:#7a3515">{t('nav_lib')}</a> ·
  <a href="/reglages" style="color:#7a3515">{t('nav_settings')}</a>
</nav>
<main style="background:#f6f1e7;color:#2b2622;padding:16px">{@render children()}</main>
