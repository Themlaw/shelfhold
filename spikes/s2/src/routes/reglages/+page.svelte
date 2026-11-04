<script lang="ts">
  import { t } from '#lib/i18n.ts';
</script>
<svelte:head><title>Shelfhold</title></svelte:head>
<h1>{t('nav_settings')}</h1>
<form>
  <label for="theme">{t('theme')}</label>
  <select id="theme"><option>Bibliothèque</option></select>
  <button type="submit" style="background:#7a3515;color:#fffdf8">{t('save')}</button>
</form>
