// The reader refuses to mount under a policy that is not its own (ADR-0055, "CSP du lecteur"). The
// entry of `/lire` carries no `<meta>` policy; a page that does, or that is a page of the SPA, or
// whose effective policy blocks what the reader's allows (an inline style) or allows what it
// forbids (a `data:` image), is not the page that was audited: nothing is mounted.

export type GuardVerdict = 'ok' | 'refused';

export interface GuardProbes {
  /** An inline `<style>` is applied: the reader's `style-src` admits `'unsafe-inline'`, the SPA's does not. */
  inlineStyleAllowed(): Promise<boolean>;
  /** A `data:` image is blocked: neither policy admits it, and none at all would. */
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
      const img = doc.createElement('img');
      let loaded = false;
      img.addEventListener('load', () => (loaded = true));
      const violated = await violationWithin(doc, /^img-src/, () => {
        img.src = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
      }, 500);
      img.remove();
      return violated && !loaded;
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
