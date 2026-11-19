import { describe, expect, it, vi } from 'vitest';
import { registerServiceWorker } from './register.ts';

describe('the registration of the service worker (SPA only)', () => {
  // req: EF-LEC-012
  it('registers the worker of the SPA at the root, as a classic script of its own origin', async () => {
    const register = vi.fn(async () => ({}));
    await registerServiceWorker({ serviceWorker: { register } }, '/bibliotheque');
    expect(register).toHaveBeenCalledWith('/service-worker.js', { scope: '/', updateViaCache: 'none' });
  });

  // req: EF-LEC-012
  it('does nothing where there is no service worker', async () => {
    await expect(registerServiceWorker({}, '/')).resolves.toBeUndefined();
  });

  // req: EF-LEC-012, ENF-SEC-004
  it('never registers from the reader (worker-src is none there)', async () => {
    const register = vi.fn(async () => ({}));
    await registerServiceWorker({ serviceWorker: { register } }, '/lire/U1');
    expect(register).not.toHaveBeenCalled();
  });

  // req: EF-LEC-012
  it('a refused registration is not an error of the screen', async () => {
    const register = vi.fn(async () => {
      throw new Error('SecurityError');
    });
    await expect(registerServiceWorker({ serviceWorker: { register } }, '/')).resolves.toBeUndefined();
  });
});
