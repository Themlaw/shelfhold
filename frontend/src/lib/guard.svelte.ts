// What a screen does with the answers of the server that are not its data: a stale password is asked
// again in place and the same action runs once more (EF-USR-022); no session and a refused role are
// said, never an empty page. Shared by the new admin screens.
import { needsReauth, needsSession, isForbidden, problemOf } from '#lib/triage.ts';

export type GuardState = 'ok' | 'signed_out' | 'forbidden' | 'failed';

/** The in-place re-authentication: ask once, replay the reserved action after, or drop it. */
export class Reauth {
  reauthMethods = $state<string[] | null>(null);
  #retry: (() => Promise<void>) | null = null;

  /** True when the server asked for a fresh password: the prompt shows and `replay` runs after it. */
  askAgain(error: unknown, replay: () => Promise<void>): boolean {
    if (!needsReauth(error)) return false;
    this.#retry = replay;
    this.reauthMethods = problemOf(error).reauth_methods ?? ['password'];
    return true;
  }

  async afterReauth(): Promise<void> {
    const again = this.#retry;
    this.#retry = null;
    this.reauthMethods = null;
    if (again) await again();
  }

  cancelReauth(): void {
    this.#retry = null;
    this.reauthMethods = null;
  }
}

export class Guard extends Reauth {
  state = $state<GuardState>('ok');

  /** Run an action; a stale password asks for it and the action runs again after. */
  async run(action: () => Promise<void>): Promise<void> {
    try {
      await action();
    } catch (error) {
      if (this.askAgain(error, () => this.run(action))) return;
      if (needsSession(error)) {
        this.state = 'signed_out';
      } else if (isForbidden(error)) {
        this.state = 'forbidden';
      } else {
        this.state = 'failed';
      }
    }
  }
}
