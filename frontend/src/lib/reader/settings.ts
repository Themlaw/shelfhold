// The settings of the reader (M5 L13, EF-LEC-006): what the profile chose. The server holds the
// priority per type — a direction pinned to a series beats the profile's mode, a comfort setting
// of one device beats the default — and the portée is asked here, when a comfort setting
// changes: « pour cet appareil » ou « partout ».
import type { ReaderSettingsOut, SettingIn } from '#lib/api/types.gen.ts';
import type { PublicationView, ReadingDirection } from './engine.ts';

export type SettingScope = 'device' | 'everywhere';
export type SettingKind = 'direction' | 'comfort';

/** What the profile chose, resolved for one series and one device. */
export interface SettingsView {
  /** `null`: the profile chose nothing, the publication's own direction wins. */
  direction: ReadingDirection | null;
  comfort: Record<string, string>;
}

/** How a page fills the screen: the one comfort setting the image reader draws with. */
export const FITS = ['contain', 'width'] as const;
export type Fit = (typeof FITS)[number];
export const DEFAULT_FIT: Fit = 'contain';

export function toSettingsView(out: ReaderSettingsOut): SettingsView {
  return { direction: out.direction, comfort: { ...out.comfort } };
}

/** The direction the reader reads with: the profile's choice for this series, else the
 * publication's own (the Œuvre's, ADR-0055). */
export function effectiveDirection(
  view: PublicationView,
  settings: SettingsView | null
): ReadingDirection {
  return settings?.direction ?? view.readingDirection;
}

/** How the reader draws a page: the choice of this device, else the default. */
export function effectiveFit(settings: SettingsView | null): Fit {
  const fit = settings?.comfort.fit;
  return FITS.includes(fit as Fit) ? (fit as Fit) : DEFAULT_FIT;
}

/** One choice waiting for its portée: the reader is asked before it is written. */
export interface PendingSetting {
  kind: SettingKind;
  key: string;
  value: string;
}

/** The body of the write, once the portée is answered: « pour cet appareil » names the device,
 * « partout » names none. */
export function settingBody(
  change: PendingSetting,
  scope: SettingScope,
  deviceId: string
): SettingIn {
  return {
    kind: change.kind,
    key: change.key,
    value: change.value,
    scope,
    ...(scope === 'device' ? { device_id: deviceId } : {})
  };
}
