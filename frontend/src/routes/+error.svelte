<script lang="ts">
  // A screen that cannot be shown (EF-UX-003): the kind of the failure decides the words, never
  // the raw message of the framework.
  import { page } from '$app/state';
  import ErrorScreen from '#lib/ui/organisms/ErrorScreen.svelte';

  const user = $derived(page.data?.user as { role?: string } | null | undefined);
  const incident = $derived((page.error as { requestId?: unknown } | null)?.requestId);
</script>

<ErrorScreen
  status={page.status}
  path={page.url.pathname}
  signedIn={Boolean(user)}
  child={user?.role === 'child'}
  online={typeof navigator === 'undefined' ? true : navigator.onLine}
  incident={typeof incident === 'string' ? incident : null}
/>
