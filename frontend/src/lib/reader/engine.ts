// The port of the reader (ADR-0055): the one thing a screen knows of how a book is drawn. A
// reader engine opens a publication, goes to a locator, says where the reader is, and lets go of
// what it holds. The engines of images (this lot) and of EPUB (L12) sit behind it; the screen
// never learns which.
import type { PublicationOut } from '#lib/api/types.gen.ts';

/** Where a reader is in a book made of pages: ADR-0055, "Format du locator", `index` from 0. */
export interface PageLocator {
  v: 1;
  type: 'page';
  index: number;
  count: number;
}

/** Where a reader is in an EPUB: ADR-0055, "Format du locator". `filter` is the version of the filter
 * that produced the DOM the CFI was taken in. */
export interface CfiLocator {
  v: 1;
  type: 'cfi';
  cfi: string;
  totalProgression: number;
  filter: string;
}

/** What an engine says and is told. */
export type Locator = PageLocator | CfiLocator;

export type ReadingDirection = 'ltr' | 'rtl' | 'webtoon';
export type Scope = 'full' | 'excerpt';

export interface PublicationEntry {
  /** The place in the book, from 0, in reading order. The server numbers its entries from 1: that
   * number never gets past `toView`, the one point of conversion. */
  index: number;
  /** A link into `/content`, ticket included: the only place a byte of a book comes from. */
  href: string;
  mediaType: string;
}

export interface PublicationView {
  kind: 'pages' | 'epub';
  scope: Scope;
  readingDirection: ReadingDirection;
  title: string;
  language: string | null;
  pages: PublicationEntry[];
  /** The reading order of an EPUB, in the same shape as `pages`. */
  spine: PublicationEntry[];
  /** The link of the package document (OPF) of an EPUB: its ticket is the one of every entry. */
  package: string | null;
  /** When the tickets of the links end, in milliseconds since the epoch; `null` when unknown. */
  expiresAt?: number | null;
}

/** The typed ways an engine is stopped; the screen words them. */
export type EngineError =
  | { kind: 'missing'; index: number }
  | { kind: 'failed'; index: number }
  | { kind: 'undisplayable'; index: number };

export interface ReaderEngine {
  /** The media types it reads. A registry asks for the guards a type needs (ADR-0055, D7). */
  readonly mediaTypes: readonly string[];
  readonly guards: Readonly<Record<string, boolean>>;
  open(publication: PublicationView, host: HTMLElement): Promise<void>;
  goTo(locator: Locator): void;
  next(): void;
  previous(): void;
  /** Says each place the reader reaches, once. Returns what stops listening. */
  onLocator(listener: (locator: Locator) => void): () => void;
  /** An extract has no page after its last one: said once, never a blank page. */
  onEnd(listener: () => void): () => void;
  onError(listener: (error: EngineError) => void): () => void;
  destroy(): void;
}

const toEntries = (entries: PublicationOut['pages']): PublicationEntry[] =>
  [...entries].sort((a, b) => a.index - b.index).map((p, place) => ({ index: place, href: p.href, mediaType: p.media_type }));

export function toView(out: PublicationOut): PublicationView {
  return {
    kind: out.kind,
    scope: out.scope,
    readingDirection: out.reading_direction,
    title: out.title,
    language: out.language,
    pages: toEntries(out.pages),
    spine: toEntries(out.spine),
    package: out.package ?? null,
    expiresAt: Number.isFinite(Date.parse(out.expires_at)) ? Date.parse(out.expires_at) : null
  };
}

export type KeyAction = 'next' | 'previous';

/** What a key does in a paged book: the arrows follow the reading direction, never the numbers. */
export function keyAction(direction: ReadingDirection, key: string): KeyAction | null {
  const forward = direction === 'rtl' ? 'ArrowLeft' : 'ArrowRight';
  const backward = direction === 'rtl' ? 'ArrowRight' : 'ArrowLeft';
  if (key === forward || key === 'PageDown' || key === ' ' || key === 'ArrowDown') return 'next';
  if (key === backward || key === 'PageUp' || key === 'ArrowUp') return 'previous';
  return null;
}
