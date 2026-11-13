import { IntlMessageFormat } from 'intl-messageformat';
import en from './en.json';
import fr from './fr.json';
import { setFormatLocale, typeset } from './format.ts';

export type MessageKey = keyof typeof fr;
export type Language = 'fr' | 'en';
type Values = Record<string, string | number>;

// The catalogs hold the same keys, one to one; the parity is checked in CI and by the backend
// tests (`test_i18n_catalogs_fr_en_same_keys`, ENF-I18N-001).
const catalogs = {
  fr,
  en
} as const satisfies Record<Language, Record<MessageKey, string>>;

let language: Language = 'fr';
const cache = new Map<string, IntlMessageFormat>();

/** The language of the interface: the profile preference, else the one of the browser. */
export function uiLanguage(): Language {
  return language;
}

/** Set the language of the interface; the next `t()` speaks it, dates and numbers follow. */
export function setUiLanguage(value: Language): void {
  language = value;
  setFormatLocale(value);
  cache.clear();
}

/** The first preferred language the catalogs speak; none: French (ENF-I18N-001). */
export function detectLanguage(languages: readonly string[] | undefined): Language {
  for (const tag of languages ?? []) {
    const primary = tag.split('-')[0]?.toLowerCase();
    if (primary === 'en') return 'en';
    if (primary === 'fr') return 'fr';
  }
  return 'fr';
}

/** Translate a catalogue key; messages use the ICU MessageFormat (plurals included). */
export function t(key: MessageKey, values?: Values): string {
  const id = `${language}:${key}`;
  let message = cache.get(id);
  if (!message) {
    message = new IntlMessageFormat(typeset(catalogs[language][key]), language);
    cache.set(id, message);
  }
  return String(message.format(values));
}

/** Whether `key` names a catalogue message: a key built from data (a setting name) may have none. */
export function hasMessage(key: string): key is MessageKey {
  return Object.hasOwn(fr, key);
}

export * from './format.ts';
