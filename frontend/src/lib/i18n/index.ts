import { IntlMessageFormat } from 'intl-messageformat';
import fr from './fr.json';

export type MessageKey = keyof typeof fr;
type Values = Record<string, string | number>;

const LOCALE = 'fr';
const cache = new Map<MessageKey, IntlMessageFormat>();

/** Translate a catalogue key; messages use the ICU MessageFormat (plurals included). */
export function t(key: MessageKey, values?: Values): string {
  let message = cache.get(key);
  if (!message) {
    message = new IntlMessageFormat(fr[key], LOCALE);
    cache.set(key, message);
  }
  return String(message.format(values));
}

/** Whether `key` names a catalogue message: a key built from data (a setting name) may have none. */
export function hasMessage(key: string): key is MessageKey {
  return Object.hasOwn(fr, key);
}
