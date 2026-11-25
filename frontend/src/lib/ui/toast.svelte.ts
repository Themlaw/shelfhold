// The notices of the app (docs/design/papier-et-encre.md, « Avis »). One at a time: a new one
// replaces the last. A notice never floats over the content: the Toast molecule prints it in
// the header on a wide screen and docks it on the tab bar on a phone, where the page keeps its
// height free. It confirms an action with the verb of the button (« Liste enregistrée »); an
// error that needs a decision is an Alert in the page, not a notice.
export type NoticeTone = 'success' | 'info' | 'warning' | 'danger';

export interface Notice {
  id: number;
  title: string;
  body?: string;
  tone: NoticeTone;
  /** a link to follow (« Voir la liste ») */
  action?: { label: string; href: string };
}

/** How long a notice stays (ms); a warning or a danger stays until it is closed. */
export const NOTICE_MS = 6000;

let next = 1;
let timer: ReturnType<typeof setTimeout> | null = null;

export const notices = $state<{ current: Notice | null }>({ current: null });

export function notify(notice: Omit<Notice, 'id' | 'tone'> & { tone?: NoticeTone }): number {
  const id = next++;
  notices.current = { tone: 'success', ...notice, id };
  if (timer) clearTimeout(timer);
  timer = null;
  const tone = notices.current.tone;
  if (tone === 'success' || tone === 'info') {
    timer = setTimeout(() => dismiss(id), NOTICE_MS);
  }
  return id;
}

/** Close the notice `id` (or the current one); a later notice is left alone. */
export function dismiss(id?: number): void {
  if (id !== undefined && notices.current?.id !== id) return;
  notices.current = null;
  if (timer) clearTimeout(timer);
  timer = null;
}
