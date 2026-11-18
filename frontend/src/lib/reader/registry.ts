// The registry of reader engines (ADR-0055, "Port ReaderEngine", D7). An engine declares the media
// types it reads and the guards it keeps in a typed field; the registry picks the engine of a type
// and refuses to be built if the guards a type demands are not declared. It cannot look into the
// options really given to a library: it checks the declared contract. M5 registers no PDF engine.
import { detectBrowser, supportsEpub, type Browser } from './browser.ts';
import type { PublicationView, ReaderEngine } from './engine.ts';

export const EPUB_TYPE = 'application/epub+zip';
const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif'] as const;

/** The guards a media type demands of the engine that reads it. */
export const REQUIRED_GUARDS: Readonly<Record<string, Readonly<Record<string, boolean>>>> = {
  [EPUB_TYPE]: { contentFilter: true },
  // CVE-2024-4367: pdf.js must never evaluate font programs. No PDF engine exists in M5 (D7).
  'application/pdf': { isEvalSupported: false }
};

export interface EngineDescriptor {
  readonly mediaTypes: readonly string[];
  readonly guards: Readonly<Record<string, boolean>>;
  /** Builds the engine; the module behind it is loaded here, on demand (ADR-0013). */
  load(): Promise<ReaderEngine>;
}

export type RefusalReason = 'browser' | 'unsupported';

export class EngineRefusedError extends Error {
  constructor(
    readonly reason: RefusalReason,
    readonly mediaType: string
  ) {
    super(`no engine for ${mediaType}: ${reason}`);
    this.name = 'EngineRefusedError';
  }
}

export interface Registry {
  readonly mediaTypes: readonly string[];
  engineFor(mediaType: string): Promise<ReaderEngine>;
}

export function createRegistry(options: { descriptors: readonly EngineDescriptor[]; browser: Browser | null }): Registry {
  const byType = new Map<string, EngineDescriptor>();
  for (const descriptor of options.descriptors) {
    for (const type of descriptor.mediaTypes) {
      for (const [guard, value] of Object.entries(REQUIRED_GUARDS[type] ?? {})) {
        if (descriptor.guards[guard] !== value) throw new Error(`the engine of ${type} does not declare ${guard}: ${String(value)}`);
      }
      byType.set(type, descriptor);
    }
  }
  return {
    mediaTypes: [...byType.keys()],
    async engineFor(mediaType) {
      const descriptor = byType.get(mediaType);
      if (!descriptor) throw new EngineRefusedError('unsupported', mediaType);
      // an iframe that inherits a policy is what the EPUB engine stands on; the images use none
      if (mediaType === EPUB_TYPE && !supportsEpub(options.browser)) throw new EngineRefusedError('browser', mediaType);
      return descriptor.load();
    }
  };
}

export interface DefaultRegistryOptions {
  browser?: Browser | null;
  renew: () => Promise<PublicationView | null>;
}

/** The registry of the app: the engine of images and the engine of EPUB, nothing else. */
export function defaultRegistry(options: DefaultRegistryOptions): Registry {
  const browser = options.browser === undefined ? detectBrowser(navigator) : options.browser;
  return createRegistry({
    browser,
    descriptors: [
      {
        mediaTypes: IMAGE_TYPES,
        guards: {},
        load: async () => (await import('./engines/images.ts')).createImageEngine({ renew: options.renew })
      },
      {
        mediaTypes: [EPUB_TYPE],
        guards: { contentFilter: true },
        load: async () => (await import('./engines/foliate.ts')).createEpubEngine({ renew: options.renew })
      }
    ]
  });
}
