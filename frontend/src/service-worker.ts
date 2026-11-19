// Entry of the service worker of the PWA shell; the behaviour is `src/lib/pwa/worker.ts`. Built by
// `@vite-pwa/sveltekit` (`vite.config.ts`), which replaces `__WB_MANIFEST` with the files of the build.
import { idbEnv } from '#lib/offline/idb.ts';
import { installPwa, versionOf, type PrecacheEntry, type PwaScope } from '#lib/pwa/worker.ts';

declare const self: PwaScope & { __WB_MANIFEST: PrecacheEntry[]; indexedDB: IDBFactory };

const manifest = self.__WB_MANIFEST;
installPwa(self, manifest, versionOf(manifest), idbEnv(self));
