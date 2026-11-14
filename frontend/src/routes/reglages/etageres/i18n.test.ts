import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import en from '#lib/i18n/en.json';
import fr from '#lib/i18n/fr.json';
import { setUiLanguage } from '#lib/i18n/index.ts';
import PlanPreview from '#lib/components/PlanPreview.svelte';
import TypedConfirm from '#lib/components/TypedConfirm.svelte';

// L45 owns two screens and two components. Whatever they say is in both catalogs, nothing they say
// is typed in the markup (ENF-I18N-001), and the family verb « Exporter » is nowhere (EF-UX-006).
const ROOT = resolve(__dirname, '../../..');
const FILES = [
  'routes/reglages/etageres/+page.svelte',
  'routes/reglages/destinations/+page.svelte',
  'lib/components/PlanPreview.svelte',
  'lib/components/TypedConfirm.svelte'
];
const read = (file: string) => readFileSync(resolve(ROOT, file), 'utf8');
// Our own component sources, not untrusted HTML: cut each script, style and comment block out by
// its delimiters, so the remaining markup is what the template renders.
const cut = (source: string, open: string, close: string) => {
  let markup = source;
  for (let start = markup.indexOf(open); start !== -1; start = markup.indexOf(open)) {
    const end = markup.indexOf(close, start);
    markup = end === -1 ? markup.slice(0, start) : markup.slice(0, start) + markup.slice(end + close.length);
  }
  return markup;
};
const strip = (source: string) => cut(cut(cut(source, '<script', '</script>'), '<style', '</style>'), '<!--', '-->');
const keysOf = (source: string) => [...source.matchAll(/\bt\(\s*'([a-z0-9_.]+)'/gi)].map((match) => match[1]!);
const asked = () => new Set(FILES.flatMap((file) => (existsSync(resolve(ROOT, file)) ? keysOf(read(file)) : [])));
const frValues = new Set(Object.values(fr as Record<string, string>));
const enValues = new Set(Object.values(en as Record<string, string>));

afterEach(() => setUiLanguage('fr'));

describe('the shelves and destinations screens speak through the catalogs', () => {
  // req: ENF-I18N-001
  it.each(FILES)('%s exists', (file) => {
    expect(existsSync(resolve(ROOT, file))).toBe(true);
  });

  // req: ENF-I18N-001
  it('every message the files ask for exists in French and in English, and is not empty', () => {
    for (const file of FILES) expect(existsSync(resolve(ROOT, file)), file).toBe(true);
    expect(asked().size).toBeGreaterThanOrEqual(40); // two screens and two components say a lot
    for (const key of asked()) {
      expect(fr, `fr: ${key}`).toHaveProperty([key]);
      expect(en, `en: ${key}`).toHaveProperty([key]);
      expect((fr as Record<string, string>)[key]!.trim(), `fr empty: ${key}`).not.toBe('');
      expect((en as Record<string, string>)[key]!.trim(), `en empty: ${key}`).not.toBe('');
    }
  });

  // req: ENF-I18N-001
  it('the English is English: a message that is the same in both is a proper noun or a figure', () => {
    for (const file of FILES) expect(existsSync(resolve(ROOT, file)), file).toBe(true);
    const same = [...asked()].filter((key) => (fr as Record<string, string>)[key] === (en as Record<string, string>)[key]);
    for (const key of same) expect((en as Record<string, string>)[key]).toMatch(/^[\p{N}\p{P}\s{}#]*$|Kavita|Suwayomi/u);
  });

  // req: ENF-I18N-001
  it.each(FILES)('%s has no text typed in its markup', (file) => {
    expect(existsSync(resolve(ROOT, file))).toBe(true);
    const markup = strip(read(file));
    const text = markup.replace(/\{[^{}]*(\{[^{}]*\}[^{}]*)*\}/g, ' ').replace(/<[^>]*>/g, ' ');
    expect(text.match(/[\p{L}]{2,}/gu) ?? []).toEqual([]);
    for (const attribute of ['aria-label', 'placeholder', 'title', 'alt', 'aria-description']) {
      for (const match of markup.matchAll(new RegExp(`\\s${attribute}="([^"]*)"`, 'g'))) {
        expect(match[1], `${attribute} in ${file}`).toMatch(/^\{.*\}$|^$/);
      }
    }
  });

  // req: EF-UX-006
  it('« Exporter » is not a verb of these screens, in either language', () => {
    for (const file of FILES) {
      expect(existsSync(resolve(ROOT, file)), file).toBe(true);
      expect(strip(read(file)), file).not.toMatch(/export/i);
      for (const key of keysOf(read(file))) {
        expect((fr as Record<string, string>)[key] ?? '', `fr ${key}`).not.toMatch(/export/i);
        expect((en as Record<string, string>)[key] ?? '', `en ${key}`).not.toMatch(/export/i);
      }
    }
  });

  // req: ENF-I18N-001
  it('the catalogs hold, word for word, the French the screens are tested with, and an English twin', () => {
    const french = [
      'Étagères', 'Destinations', 'Aperçu du nommage', 'Renommer', 'Appliquer le plan', 'Retirer', 'Détacher Kavita',
      'Conserver les fichiers', 'Supprimer les fichiers', 'Voir ce qui sera perdu', 'Rapport final', 'Laissé en place',
      'Recopiez la phrase pour confirmer', 'Confirmé par Kavita', 'Contesté', 'Non vérifié par Kavita',
      'Destination retirée.', 'Retrait inachevé : reprenez-le.', 'Détachement inachevé : reprenez-le.',
      'Kavita fonctionne sans Shelfhold.', 'Réglages de Kavita restaurés', 'Réglages de Kavita non restaurés',
      'Clé dédiée révoquée', 'Clé dédiée non révoquée', 'Mot de passe du compte Kavita dédié',
      'Effacer les données cachées', 'Effacer le journal des pierres tombales', 'Rien à renommer.',
      "Aucune destination n'est reliée à une étagère.", "Ces écrans sont réservés à l'administrateur.",
      'Le plan a changé : recalculez-le.', 'Le retrait a été refusé.', 'Le détachement a été refusé.',
      'Chapitre', 'Tome', 'Spécial', 'Dossier des spéciaux', 'Segment', 'Renommages', 'Annuler', 'Confirmer'
    ];
    for (const word of french) expect(frValues.has(word), `fr: ${word}`).toBe(true);
    const english = [
      'Shelves', 'Destinations', 'Naming preview', 'Rename', 'Apply the plan', 'Remove', 'Detach Kavita',
      'Keep the files', 'Delete the files', 'See what will be lost', 'Final report', 'Left in place',
      'Type the phrase to confirm', 'Confirmed by Kavita', 'Contested', 'Not verified by Kavita',
      'Destination removed.', 'Removal unfinished: resume it.', 'Detachment unfinished: resume it.',
      'Kavita works without Shelfhold.', 'Kavita settings restored', 'Kavita settings not restored',
      'Dedicated key revoked', 'Dedicated key not revoked', 'Password of the dedicated Kavita account',
      'Erase the hidden data', 'Erase the tombstone journal', 'Nothing to rename.',
      'No destination is linked to a shelf.', 'These screens are reserved to the administrator.',
      'The plan changed: compute it again.', 'The removal was refused.', 'The detachment was refused.',
      'Chapter', 'Volume', 'Special', 'Specials folder', 'Segment', 'Renames', 'Cancel', 'Confirm'
    ];
    for (const word of english) expect(enValues.has(word), `en: ${word}`).toBe(true);
  });

  // req: ENF-I18N-001
  it('the typed confirmation speaks English when the interface does', () => {
    setUiLanguage('en');
    render(TypedConfirm, { phrase: 'REMOVE manga/', onconfirm: () => {}, oncancel: () => {} } as never);
    expect(screen.getByLabelText('Type the phrase to confirm')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy();
  });

  // req: ENF-I18N-001, EF-EXP-037
  it('the plan preview speaks English, warning included, and its phrase is the English one', () => {
    setUiLanguage('en');
    const plan = {
      plan_id: '01JPLAN0000000000000000R01', base_version: '1', batches: 1, blocked: [],
      steps: [{ kind: 'file', src: 'A/a.cbz', dst: 'A/b.cbz', export_item_id: '01JITEM00000000000000000A1' }],
      warnings: [{ code: 'progress_loss', readers: 7, exact: true, series: ['A'] }]
    };
    render(PlanPreview, { plan, onapply: () => {}, oncancel: () => {} } as never);
    expect(screen.getByRole('alert')).toHaveTextContent('The Kavita progress of 7 readers will be lost.');
    expect(screen.getByRole('list', { name: 'Renames' })).toBeTruthy();
    expect(screen.getByText('LOSE THE PROGRESS')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Apply the plan' })).toBeDisabled();
  });
});
