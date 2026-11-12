// The closed list of layout variants a theme may choose (I51, ADR-0051). A component reads them as
// CSS custom properties (`var(--variant-cover-layout)`), never as the name of the theme.
export const VARIANT_POINTS = {
  density: ['compact', 'comfortable'],
  cover_layout: ['grid', 'list'],
  series_header: ['banner', 'compact'],
  nav_width: ['narrow', 'wide']
} as const;

export type VariantPoint = keyof typeof VARIANT_POINTS;
export type VariantValue<P extends VariantPoint> = (typeof VARIANT_POINTS)[P][number];

/** The custom property that carries a variant point, as compiled from `tokens.json`. */
export function variantProperty(point: VariantPoint): string {
  return `--variant-${point.replace('_', '-')}`;
}
