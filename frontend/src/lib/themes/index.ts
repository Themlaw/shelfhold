// The theme mechanism (EF-UX-012, ADR-0051): a theme is a set of tokens and variants; the root
// element carries `data-theme` and, when the user chose one, `data-color-scheme`. Only this folder
// and the root layout know the names: no component, screen or store reads them (I51).
export const THEMES = ['bibliotheque'] as const;
export type Theme = (typeof THEMES)[number];
export const DEFAULT_THEME: Theme = 'bibliotheque';

export const COLOR_SCHEMES = ['auto', 'light', 'dark'] as const;
export type ColorScheme = (typeof COLOR_SCHEMES)[number];

export type Appearance = { theme: Theme; colorScheme: ColorScheme };

/** The key the boot script of `app.html` reads before the first render. */
export const STORAGE_KEY = 'shelfhold.appearance';

const isTheme = (value: unknown): value is Theme => THEMES.some((t) => t === value);
const isScheme = (value: unknown): value is ColorScheme => COLOR_SCHEMES.some((c) => c === value);

/** What the client knows of an appearance: anything outside the lists falls back to the default. */
export function sanitize(value: { theme?: unknown; colorScheme?: unknown } | null): Appearance {
  return {
    theme: isTheme(value?.theme) ? value.theme : DEFAULT_THEME,
    colorScheme: isScheme(value?.colorScheme) ? value.colorScheme : 'auto'
  };
}

export function applyAppearance(root: HTMLElement, appearance: Appearance): void {
  const { theme, colorScheme } = sanitize(appearance);
  root.setAttribute('data-theme', theme);
  if (colorScheme === 'auto') root.removeAttribute('data-color-scheme');
  else root.setAttribute('data-color-scheme', colorScheme);
}

export function rememberAppearance(storage: Storage, appearance: Appearance): void {
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(sanitize(appearance)));
  } catch {
    // storage blocked: the next visit starts from the default, then the server's answer applies
  }
}

export function loadAppearance(storage: Storage): Appearance {
  try {
    return sanitize(JSON.parse(storage.getItem(STORAGE_KEY) ?? 'null'));
  } catch {
    return sanitize(null);
  }
}
