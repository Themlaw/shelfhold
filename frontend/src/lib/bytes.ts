// A size in bytes, written the way the person's language writes it (Intl units: « 150 Mo »,
// « 150 MB »). Binary steps (1024), one decimal at most, never a scientific notation.
import { formatNumber } from '#lib/i18n/format.ts';

const STEPS = ['byte', 'kilobyte', 'megabyte', 'gigabyte', 'terabyte'] as const;

export function formatBytes(bytes: number): string {
  let value = Math.max(0, bytes);
  let step = 0;
  while (value >= 1024 && step < STEPS.length - 1) {
    value /= 1024;
    step += 1;
  }
  return formatNumber(value, {
    style: 'unit',
    unit: STEPS[step],
    unitDisplay: 'short',
    maximumFractionDigits: step === 0 ? 0 : 1
  });
}
