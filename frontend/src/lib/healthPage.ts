// The Health page (ENF-OBS-005, EF-INT-025) as the screens read it. The server types its nested
// documents loosely, so each one is narrowed here, field by field: an entry that does not have the
// expected shape is dropped, never displayed half-way. Nothing in a document is a secret or a path.
import type { HealthPageOut } from '#lib/api/types.gen.ts';

export interface HealthAction {
  command: string;
  reserved: boolean;
  text: string;
}

export interface SourceEntry {
  pluginId: string;
  state: 'ok' | 'paused' | 'degraded';
  code: string | null;
  action: HealthAction | null;
}

export interface Entry {
  code: string;
  subject: string | null;
  action: HealthAction | null;
}

export interface HealthView {
  sources: SourceEntry[];
  irreplaceable: {
    count: number;
    withoutKeptCopy: number;
    singleCopy: number;
    backupAgeDays: number | null;
  };
  lostOriginals: Entry[];
  backup: { lastOkAt: string | null; warnings: Entry[] };
  withdrawalPending: { count: number; action: HealthAction | null };
  kavitaAudit: Entry[];
  libraryTypeChanges: Entry[];
  jurisdiction: { country: string; confirmed: boolean; action: HealthAction | null } | null;
  stats: { waitingStates: number; unitsExported: number; arrivalsPending: number };
}

type Doc = Record<string, unknown>;

const isDoc = (value: unknown): value is Doc =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const str = (value: unknown): string | null => (typeof value === 'string' && value ? value : null);
const num = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? value : 0);
const docs = (value: unknown): Doc[] => (Array.isArray(value) ? value.filter(isDoc) : []);

function action(value: unknown): HealthAction | null {
  if (!isDoc(value)) return null;
  const command = str(value.command);
  if (command === null) return null;
  return { command, reserved: value.reserved === true, text: str(value.text) ?? '' };
}

function entries(value: unknown): Entry[] {
  const found: Entry[] = [];
  for (const doc of docs(value)) {
    const code = str(doc.code);
    if (code !== null) found.push({ code, subject: str(doc.subject), action: action(doc.action) });
  }
  return found;
}

const STATES = ['ok', 'paused', 'degraded'] as const;

function sources(value: HealthPageOut['sources']): SourceEntry[] {
  const found: SourceEntry[] = [];
  for (const source of value) {
    const state = STATES.find((s) => s === source.state);
    if (state === undefined) continue;
    found.push({
      pluginId: source.plugin_id,
      state,
      code: source.code ?? null,
      action: action(source.action)
    });
  }
  return found;
}

export function healthView(page: HealthPageOut): HealthView {
  const irreplaceable = page.irreplaceable;
  const store = isDoc(irreplaceable.store_backup) ? irreplaceable.store_backup : {};
  const waiting = isDoc(page.stats.human_interventions) ? page.stats.human_interventions : {};
  const jurisdiction = page.jurisdiction;
  return {
    sources: sources(page.sources),
    irreplaceable: {
      count: num(irreplaceable.count),
      withoutKeptCopy: num(irreplaceable.without_kept_copy),
      singleCopy: num(irreplaceable.single_copy),
      backupAgeDays: typeof store.age_days === 'number' ? store.age_days : null
    },
    lostOriginals: entries(page.lost_originals),
    backup: { lastOkAt: str(page.backup.last_ok_at), warnings: entries(page.backup.warnings) },
    withdrawalPending: {
      count: num(page.withdrawal_pending.count),
      action: action(page.withdrawal_pending.action)
    },
    kavitaAudit: entries(page.kavita_audit),
    libraryTypeChanges: entries(page.library_type_changes),
    jurisdiction:
      jurisdiction !== null && str(jurisdiction.country) !== null
        ? {
            country: str(jurisdiction.country) ?? '',
            confirmed: jurisdiction.confirmed === true,
            action: action(jurisdiction.action)
          }
        : null,
    stats: {
      waitingStates: Object.values(waiting).reduce<number>((sum, n) => sum + num(n), 0),
      unitsExported: num(page.stats.units_exported),
      arrivalsPending: num(page.stats.arrivals_pending)
    }
  };
}
