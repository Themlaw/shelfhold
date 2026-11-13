// Formatting (ENF-I18N-003). Every date and number goes through `Intl` in the language of the
// interface (`fr-FR` or `en-US`): month names, a comma for decimals in French, and the narrow
// no-break space (U+202F) as the thousands separator. The typography of the catalogue texts is
// applied here too (`typeset`).
const NARROW_NBSP = String.fromCharCode(0x202f);
const BEFORE_PUNCTUATION = new RegExp(`[ ${String.fromCharCode(0xa0)}${NARROW_NBSP}]+(?=[;:!?])`, 'g');

const LOCALES = { fr: 'fr-FR', en: 'en-US' } as const;
let locale: string = LOCALES.fr;

/** Set the locale of every formatted date and number (the language of the interface). */
export function setFormatLocale(language: 'fr' | 'en'): void {
  locale = LOCALES[language];
}

type DateInput = Date | string | number;

const toDate = (value: DateInput): Date => (value instanceof Date ? value : new Date(value));

export function formatDate(value: DateInput, options?: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(locale, options).format(toDate(value));
}

export function formatDateTime(value: DateInput, options?: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short', ...options }).format(toDate(value));
}

export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(locale, options).format(value);
}

export function formatPercent(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(locale, { style: 'percent', ...options }).format(value);
}

export function formatCurrency(value: number, currency = 'EUR', options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(locale, { style: 'currency', currency, ...options }).format(value);
}

/**
 * A plain or no-break space before `;`, `:`, `!` or `?` becomes a narrow no-break space, except
 * in a code span between backticks. Applied to a catalogue message, never to a value.
 */
export function typeset(message: string): string {
  return message
    .split(/(`[^`]*`)/)
    .map((part, index) => (index % 2 ? part : part.replace(BEFORE_PUNCTUATION, NARROW_NBSP)))
    .join('');
}
