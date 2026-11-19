// « Lire hors ligne » (M5 L16b, EF-LEC-012): keep a publication the profile may read, for that
// profile. The links of a publication carry a ticket that lives ten minutes: every entry is fetched
// now and kept under a name that holds no ticket (`copyUrl`); the manifest kept is the publication
// with those names. An extract is never kept (D-P1). A failed download leaves nothing behind.
import type { PublicationOut } from '#lib/api/types.gen.ts';
import { copyUrl } from './env.ts';
import type { CopyEntry, OfflineStore } from './store.ts';
import { syncSince, type OfflineApi } from './sync.ts';

export type KeepFailure = 'excerpt' | 'not_readable' | 'download_failed';

export class OfflineKeepError extends Error {
  constructor(readonly reason: KeepFailure) {
    super(`the copy cannot be kept: ${reason}`);
  }
}

export type KeepApi = OfflineApi & { entry(url: string): Promise<Response> };

const TEN_YEARS_MS = 10 * 365 * 24 * 3600 * 1000;
const CONTENT_LINK = /^\/content\/[^/]+\/(.+)$/;
const MAX_ENTRIES = 2000;

/** The `href` of every item of the manifest of a package document (a name relative to it). */
function manifestHrefs(opf: string): string[] {
  const doc = new DOMParser().parseFromString(opf, 'application/xml');
  return [...doc.getElementsByTagName('item')].map((item) => item.getAttribute('href') ?? '').filter((href) => href !== '');
}

/** The relative `url(...)` and `@import` names of a style sheet (fonts, images). */
function styleUrls(css: string): string[] {
  const found: string[] = [];
  for (const match of css.matchAll(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s]*))\s*\)|@import\s+(?:"([^"]*)"|'([^']*)')/gi)) {
    const href = match[1] ?? match[2] ?? match[3] ?? match[4] ?? match[5] ?? '';
    if (href !== '' && !href.startsWith('data:')) found.push(href);
  }
  return found;
}

/** A name relative to a link of the content route, as a link of the same ticket; null when it climbs
 * out of it, names another origin or is not a path (the same rule as `entryName`). */
function resolveLink(from: string, href: string): string | null {
  const base = /^(\/content\/[^/]+\/)/.exec(from)?.[1];
  if (!base || /^[a-z][a-z0-9+.-]*:|^[/\\]|^\/\//i.test(href)) return null;
  const path = new URL(href.split(/[?#]/)[0] ?? '', `https://content.invalid${from}`).pathname;
  if (!path.startsWith(base) || path === base) return null;
  try {
    entryName(path);
  } catch {
    return null;
  }
  return path;
}

function entryName(href: string): string {
  const match = CONTENT_LINK.exec(href.split(/[?#]/)[0] ?? '');
  if (!match) throw new OfflineKeepError('download_failed');
  const name = match[1]!.split('/').map(decodeURIComponent).join('/');
  if (name.split('/').some((part) => part === '' || part === '.' || part === '..')) throw new OfflineKeepError('download_failed');
  return name;
}

export async function keepCopy(options: {
  store: OfflineStore;
  profileId: string;
  api: KeepApi;
  carrierType: string;
  carrierId: string;
}): Promise<{ workId: string; carrierType: string; carrierId: string }> {
  const { store, profileId, api } = options;
  // the token first: whatever changes after it is a removal the server will name; before the copy
  // is made, never after (a Work created after a token is not in its removals)
  if (!(await syncSince({ store, profileId, api }, { evicted: [], wiped: false }))) throw new OfflineKeepError('download_failed');
  if ((await store.activeProfile()) !== profileId) throw new OfflineKeepError('not_readable');
  let asked: Awaited<ReturnType<OfflineApi['publication']>>;
  try {
    asked = await api.publication(options.carrierType, options.carrierId);
  } catch {
    throw new OfflineKeepError('download_failed');
  }
  if (asked.status !== 200 || !asked.publication) throw new OfflineKeepError(asked.status >= 500 ? 'download_failed' : 'not_readable');
  const publication = asked.publication;
  if (publication.scope !== 'full') throw new OfflineKeepError('excerpt');

  const type = publication.carrier_type;
  const rewrite = (href: string) => copyUrl(type, publication.carrier_id, entryName(href));
  const entries = [...publication.pages, ...publication.spine];
  const links = [...entries.map((e) => e.href), ...(publication.package ? [publication.package] : [])];
  const kept: PublicationOut = {
    ...publication,
    expires_at: new Date(Date.now() + TEN_YEARS_MS).toISOString(),
    pages: publication.pages.map((e) => ({ ...e, href: rewrite(e.href) })),
    spine: publication.spine.map((e) => ({ ...e, href: rewrite(e.href) })),
    package: publication.package ? rewrite(publication.package) : null
  };
  links.map(rewrite); // throws before anything is fetched on a link that is not the content route

  async function* download(): AsyncGenerator<CopyEntry> {
    // the links of the publication, then what the package of an EPUB names (images, styles, fonts,
    // the navigation document) and what its style sheets name, never past a bound
    const queue = [...links];
    const seen = new Set(queue);
    const add = (link: string | null) => {
      if (link !== null && !seen.has(link) && seen.size < MAX_ENTRIES) {
        seen.add(link);
        queue.push(link);
      }
    };
    for (let link = queue.shift(); link !== undefined; link = queue.shift()) {
      const response = await api.entry(link);
      if (response.status !== 200 || response.redirected) throw new OfflineKeepError('download_failed');
      if (link === publication.package) for (const href of manifestHrefs(await response.clone().text())) add(resolveLink(link, href));
      else if (/\.css(?:[?#]|$)/i.test(link)) for (const href of styleUrls(await response.clone().text())) add(resolveLink(link, href));
      yield { url: rewrite(link), response };
    }
  }

  try {
    await store.keep(profileId, { carrierType: type as 'content' | 'export_item', carrierId: publication.carrier_id, workId: publication.work_id, title: publication.title, publication: kept, ...(options.carrierType === 'unit' ? { via: options.carrierId } : {}) }, download());
  } catch (error) {
    throw error instanceof OfflineKeepError ? error : new OfflineKeepError('download_failed');
  }
  return { workId: publication.work_id, carrierType: type, carrierId: publication.carrier_id };
}
