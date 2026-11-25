// How a cover without an image is composed (docs/design/papier-et-encre.md, « Couvertures »):
// everything is derived from the identifier of the series (its title when there is none), so the
// same work always gets the same cover, on every device, without storing anything. Pure
// functions, tested on their own.
import { SCENES, type Scene } from "./cover-art.ts";
import { MOTIFS, type MotifName } from "./icons.ts";

export type CoverKind = "book" | "manga" | "webtoon" | "comic";
/** The compositions of a bound novel: title and motif, title piece, medallion, « collection blanche ». */
export type BookLayout = "a" | "b" | "c" | "blanche";

export interface CoverPlan {
  family: CoverKind;
  /** paper of the cloth (1 to 8), for book, webtoon and comic */
  paper: number;
  layout: BookLayout;
  motif: MotifName;
  /** the one colour of a manga band or of a « collection blanche » title */
  ink: "red" | "blue" | "green" | "vermilion" | "teal" | "plum";
  /** the drawn scene of a manga, webtoon or comic cover (null for a book) */
  scene: Scene | null;
}

/** A stable 32-bit hash (FNV-1a) of a string. */
export function hash(text: string): number {
  let h = 0x811c9dc5;
  for (const char of text) {
    h ^= char.codePointAt(0) ?? 0;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

const LAYOUTS: readonly BookLayout[] = ["a", "b", "c", "blanche"];
const MOTIF_NAMES = Object.keys(MOTIFS) as MotifName[];
const BOOK_INKS = ["red", "blue", "green"] as const;
const MANGA_INKS = ["vermilion", "teal", "plum"] as const;

/** The kind of a work, from the media names the server uses; anything else is a book. */
export function coverKind(media: string | null | undefined): CoverKind {
  const value = (media ?? "").toLowerCase();
  if (/manga|manhwa|manhua/.test(value)) return "manga";
  if (/webtoon|webcomic/.test(value)) return "webtoon";
  if (/comic|bd|bande|album|graphic/.test(value)) return "comic";
  return "book";
}

/** The plan of a generated cover. `seed` is the identifier of the series, or its title. */
export function coverPlan(seed: string, kind: CoverKind = "book"): CoverPlan {
  const h = hash(seed.trim().toLocaleLowerCase());
  const scenes = kind === "book" ? null : SCENES[kind];
  return {
    family: kind,
    paper: (h % 8) + 1,
    layout: LAYOUTS[(h >>> 3) % LAYOUTS.length]!,
    motif: MOTIF_NAMES[(h >>> 5) % MOTIF_NAMES.length]!,
    ink:
      kind === "manga" ? MANGA_INKS[(h >>> 9) % 3]! : BOOK_INKS[(h >>> 9) % 3]!,
    scene: scenes ? scenes[(h >>> 11) % scenes.length]! : null,
  };
}
