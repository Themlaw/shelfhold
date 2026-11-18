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

/** What an engine says and is told. EPUB adds its own member (L12), without touching a screen. */
export type Locator = PageLocator;

export type ReadingDirection = 'ltr' | 'rtl' | 'webtoon';
export type Scope = 'full' | 'excerpt';

export interface PublicationEntry {
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
  /** The Œuvre: the anchor of a setting pinned to this series (L13). */
  seriesId: string;
  pages: PublicationEntry[];
}

/** The typed ways an engine is stopped; the screen words them. */
export type EngineError = { kind: 'missing'; index: number } | { kind: 'failed'; index: number };

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

export function toView(out: PublicationOut): PublicationView {
  return {
    kind: out.kind,
    scope: out.scope,
    readingDirection: out.reading_direction,
    title: out.title,
    language: out.language,
    seriesId: out.series_id,
    pages: out.pages.map((p) => ({ index: p.index, href: p.href, mediaType: p.media_type }))
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
