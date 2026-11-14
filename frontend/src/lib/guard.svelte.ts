// What a screen does with the answers of the server that are not its data: a stale password is asked
// again in place and the same action runs once more (EF-USR-022); no session and a refused role are
// said, never an empty page. Shared by the new admin screens.
import { needsReauth, needsSession, isForbidden, problemOf } from '#lib/triage.ts';

export type GuardState = 'ok' | 'signed_out' | 'forbidden' | 'failed';

export class Guard {
  state = $state<GuardState>('ok');
  reauthMethods = $state<string[] | null>(null);
  #retry: (() => Promise<void>) | null = null;

  /** Run an action; a stale password asks for it and the action runs again after. */
  async run(action: () => Promise<void>): Promise<void> {
    try {
      await action();
    } catch (error) {
      if (needsReauth(error)) {
        this.#retry = action;
        this.reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else if (needsSession(error)) {
        this.state = 'signed_out';
      } else if (isForbidden(error)) {
        this.state = 'forbidden';
      } else {
        this.state = 'failed';
      }
    }
  }

  async afterReauth(): Promise<void> {
    const again = this.#retry;
    this.#retry = null;
    this.reauthMethods = null;
    if (again) await this.run(again);
  }

  cancelReauth(): void {
    this.#retry = null;
    this.reauthMethods = null;
  }
}
