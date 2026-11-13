// French formatting (ENF-I18N-003). Every date and number goes through `Intl` with `fr-FR`:
// month names in French, a comma for decimals, and the narrow no-break space (U+202F) as the
// thousands separator. The typography of the catalogue texts is applied here too (`typeset`).
const LOCALE = 'fr-FR';
const NARROW_NBSP = String.fromCharCode(0x202f);
const BEFORE_PUNCTUATION = new RegExp(`[ ${String.fromCharCode(0xa0)}${NARROW_NBSP}]+(?=[;:!?])`, 'g');

type DateInput = Date | string | number;

const toDate = (value: DateInput): Date => (value instanceof Date ? value : new Date(value));

export function formatDate(value: DateInput, options?: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(LOCALE, options).format(toDate(value));
}

export function formatDateTime(value: DateInput, options?: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(LOCALE, { dateStyle: 'medium', timeStyle: 'short', ...options }).format(toDate(value));
}

export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(LOCALE, options).format(value);
}

export function formatPercent(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(LOCALE, { style: 'percent', ...options }).format(value);
}

export function formatCurrency(value: number, currency = 'EUR', options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(LOCALE, { style: 'currency', currency, ...options }).format(value);
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
