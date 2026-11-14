import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import en from '#lib/i18n/en.json';
import fr from '#lib/i18n/fr.json';

// L42 owns four components and one screen. Whatever they say is in both catalogs, nothing they
// say is typed in the markup (ENF-I18N-001), and no family verb is « Exporter » (EF-UX-006).
const ROOT = resolve(__dirname, '../..');
const FILES = [
  'routes/chercher/+page.svelte',
  'lib/components/AddSheet.svelte',
  'lib/components/TrustCard.svelte',
  'lib/components/SearchedIn.svelte'
];
const read = (file: string) => readFileSync(resolve(ROOT, file), 'utf8');
// Our own component sources, not untrusted HTML: drop the script, style and comment blocks until
// nothing is left to drop, so the remaining markup is what the template renders.
const strip = (source: string) => {
  let previous: string;
  let markup = source;
  do {
    previous = markup;
    markup = markup
      .replace(/<script\b[\s\S]*?<\/script\s*>/gi, '')
      .replace(/<style\b[\s\S]*?<\/style\s*>/gi, '')
      .replace(/<!--[\s\S]*?--!?>/g, '');
  } while (markup !== previous);
  return markup;
};
const keysOf = (source: string) =>
  [...source.matchAll(/\bt\(\s*'([a-z0-9_.]+)'/gi)].map((match) => match[1]!);

describe('the Search screen and the Add sheet speak through the catalogs', () => {
  // req: ENF-I18N-001
  it.each(FILES)('%s exists', (file) => {
    expect(existsSync(resolve(ROOT, file))).toBe(true);
  });

  // req: ENF-I18N-001
  it('every message the files ask for exists in French and in English, and is not empty', () => {
    const asked = new Set(FILES.flatMap((file) => (existsSync(resolve(ROOT, file)) ? keysOf(read(file)) : [])));
    expect(asked.size).toBeGreaterThanOrEqual(20); // the screen says more than a handful of things
    for (const key of asked) {
      expect(fr, `fr: ${key}`).toHaveProperty([key]);
      expect(en, `en: ${key}`).toHaveProperty([key]);
      expect((fr as Record<string, string>)[key]!.trim(), `fr empty: ${key}`).not.toBe('');
      expect((en as Record<string, string>)[key]!.trim(), `en empty: ${key}`).not.toBe('');
    }
  });

  // req: ENF-I18N-001
  it('the English is English: a message that is the same in both is a proper noun or a figure', () => {
    for (const file of FILES) expect(existsSync(resolve(ROOT, file)), file).toBe(true);
    const asked = new Set(FILES.flatMap((file) => (existsSync(resolve(ROOT, file)) ? keysOf(read(file)) : [])));
    const same = [...asked].filter(
      (key) => (fr as Record<string, string>)[key] === (en as Record<string, string>)[key]
    );
    for (const key of same) expect((en as Record<string, string>)[key]).toMatch(/^[\p{N}\p{P}\s{}#]*$|Kavita/u);
  });

  // req: ENF-I18N-001
  it.each(FILES)('%s has no text typed in its markup', (file) => {
    expect(existsSync(resolve(ROOT, file))).toBe(true);
    const markup = strip(read(file));
    // text between tags, outside any {expression}
    const text = markup.replace(/\{[^{}]*(\{[^{}]*\}[^{}]*)*\}/g, ' ').replace(/<[^>]*>/g, ' ');
    expect(text.match(/[\p{L}]{2,}/gu) ?? []).toEqual([]);
    // visible attributes are expressions, not words
    for (const attribute of ['aria-label', 'placeholder', 'title', 'alt', 'aria-description']) {
      for (const match of markup.matchAll(new RegExp(`\\s${attribute}="([^"]*)"`, 'g'))) {
        expect(match[1], `${attribute} in ${file}`).toMatch(/^\{.*\}$|^$/);
      }
    }
  });

  // req: EF-UX-006
  it('« Exporter » is not a verb of this screen: no message it asks for says it, in either language', () => {
    for (const file of FILES) {
      expect(existsSync(resolve(ROOT, file)), file).toBe(true);
      expect(strip(read(file)), file).not.toMatch(/export/i);
      for (const key of keysOf(read(file))) {
        expect((fr as Record<string, string>)[key] ?? '', `fr ${key}`).not.toMatch(/export/i);
        expect((en as Record<string, string>)[key] ?? '', `en ${key}`).not.toMatch(/export/i);
      }
    }
  });

  // req: EF-UX-006
  it('the three verbs of the contract are the catalog\'s, word for word', () => {
    const values = (catalog: Record<string, string>) => new Set(Object.values(catalog));
    for (const word of ['Ajouter à ma bibliothèque', 'Demander', 'Ouvrir dans Kavita', 'Vider la corbeille']) {
      expect(values(fr as Record<string, string>).has(word), word).toBe(true);
    }
    for (const word of ['Add to my library', 'Request', 'Open in Kavita', 'Empty the trash']) {
      expect(values(en as Record<string, string>).has(word), word).toBe(true);
    }
  });
});
