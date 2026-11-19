// The reader refuses to mount under a policy that is not its own (ADR-0055, "CSP du lecteur"). The
// entry of `/lire` carries no `<meta>` policy; a page that does, or that is a page of the SPA, or
// whose effective policy blocks what the reader's allows (an inline style) or allows what it
// forbids (the header of the page lets a `data:` image through), is not the page that was audited:
// nothing is mounted. The second is read from the header itself: a probe that provokes a refusal
// would put a policy violation in the console of every reading.

export type GuardVerdict = 'ok' | 'refused';

export interface GuardProbes {
  /** An inline `<style>` is applied: the reader's `style-src` admits `'unsafe-inline'`, the SPA's does not. */
  inlineStyleAllowed(): Promise<boolean>;
  /** The policy the server sent for this page exists and admits no `data:` image nor wildcard. */
  forbiddenImageBlocked(): Promise<boolean>;
}

const SPA_MARKERS = '[data-sveltekit-preload-data], [data-sveltekit-reload], script[src*="/_app/"], link[href*="/_app/"]';

/** Waits for a policy violation of a kind, or for the resource to settle, whichever is first. */
function violationWithin(doc: Document, directive: RegExp, act: () => Promise<void> | void, ms = 1500): Promise<boolean> {
  return new Promise((resolve) => {
    let done = false;
    const finish = (value: boolean) => {
      if (done) return;
      done = true;
      doc.removeEventListener('securitypolicyviolation', onViolation);
      resolve(value);
    };
    const onViolation = (event: Event) => {
      if (directive.test((event as SecurityPolicyViolationEvent).effectiveDirective)) finish(true);
    };
    doc.addEventListener('securitypolicyviolation', onViolation);
    Promise.resolve(act()).then(
      () => setTimeout(() => finish(false), ms),
      () => finish(false)
    );
  });
}

export function browserProbes(doc: Document = document): GuardProbes {
  return {
    async inlineStyleAllowed() {
      const style = doc.createElement('style');
      style.textContent = '#shelfhold-reader-probe{width:7px}';
      const el = doc.createElement('div');
      el.id = 'shelfhold-reader-probe';
      el.style.cssText = 'position:absolute;visibility:hidden';
      const blocked = violationWithin(doc, /^style-src/, () => {
        doc.head.append(style);
        doc.body.append(el);
      }, 300);
      const violated = await blocked;
      const applied = doc.defaultView?.getComputedStyle(el).width === '7px';
      style.remove();
      el.remove();
      return !violated && applied;
    },
    async forbiddenImageBlocked() {
      const view = doc.defaultView;
      if (!view?.fetch) return false;
      const response = await view.fetch(view.location.href, {
        credentials: 'same-origin',
        referrerPolicy: 'no-referrer',
        redirect: 'error'
      });
      void response.body?.cancel();
      const policy = response.headers.get('content-security-policy') ?? '';
      const images = policy
        .split(';')
        .map((part) => part.trim().split(/\s+/))
        .find(([name]) => name === 'img-src');
      if (!images) return false; // `default-src 'none'` alone is not what the reader sends
      return !images.slice(1).some((source) => source === '*' || source.startsWith('data:'));
    }
  };
}

export async function checkReaderCsp(doc: Document = document, probes: GuardProbes = browserProbes(doc)): Promise<GuardVerdict> {
  for (const meta of Array.from(doc.querySelectorAll('meta'))) {
    if (meta.getAttribute('http-equiv')?.trim().toLowerCase() === 'content-security-policy') return 'refused';
  }
  if (doc.querySelector(SPA_MARKERS) || doc.body?.matches(SPA_MARKERS)) return 'refused';
  try {
    if (!(await probes.inlineStyleAllowed())) return 'refused';
    if (!(await probes.forbiddenImageBlocked())) return 'refused';
  } catch {
    return 'refused';
  }
  return 'ok';
}
