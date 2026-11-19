// The registration of the service worker, from the SPA only (ADR-0055: the reader, `/lire`, has
// `worker-src 'none'` and registers nothing). The worker is a classic script of the origin, at the
// root; the HTTP cache is never used to check for a new one.

interface WorkerHost {
  serviceWorker?: { register: (url: string, options: { scope: string; updateViaCache: 'none' }) => Promise<unknown> };
}

export async function registerServiceWorker(host: WorkerHost, pathname: string): Promise<void> {
  if (!host.serviceWorker) return;
  if (pathname === '/lire' || pathname.startsWith('/lire/')) return;
  try {
    await host.serviceWorker.register('/service-worker.js', { scope: '/', updateViaCache: 'none' });
  } catch {
    // no worker (private window, blocked by the browser): the app works the same, online
  }
}
