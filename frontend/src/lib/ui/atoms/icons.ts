// The icons of « Papier et encre » (docs/design/papier-et-encre.md): one stroke (1.6 on a 24 grid),
// round caps, drawn for this project; no icon font, no host. Rendered by Icon.svelte as SVG
// elements, never as an HTML string. The motifs (40 grid) are engraved on generated covers.
export type Shape =
  | { tag: 'path'; d: string }
  | { tag: 'circle'; cx: number; cy: number; r: number }
  | { tag: 'rect'; x: number; y: number; width: number; height: number; rx?: number }
  | { tag: 'ellipse'; cx: number; cy: number; rx: number; ry: number };

export const ICONS = {
  "home":[{"tag":"path","d":"M3 11 12 4l9 7"},{"tag":"path","d":"M5 9.5V20h14V9.5"},{"tag":"path","d":"M10 20v-6h4v6"}],
  "books":[{"tag":"path","d":"M4 4h4v16H4z"},{"tag":"path","d":"M10 4h4v16h-4z"},{"tag":"path","d":"m16 5.2 3.4-.9 3.1 15-3.4.9z"}],
  "search":[{"tag":"circle","cx":11,"cy":11,"r":6.5},{"tag":"path","d":"m20 20-4.2-4.2"}],
  "inbox":[{"tag":"path","d":"M4 13h4l1.5 3h5l1.5-3h4"},{"tag":"path","d":"M5.5 5h13L21 13v6H3v-6z"}],
  "stamp":[{"tag":"circle","cx":12,"cy":12,"r":9},{"tag":"path","d":"m8 12.5 2.7 2.7L16 9.8"}],
  "users":[{"tag":"circle","cx":9,"cy":8,"r":3.5},{"tag":"path","d":"M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"},{"tag":"path","d":"M16 4.6a3.5 3.5 0 0 1 0 6.8"},{"tag":"path","d":"M18 14.2c1.9.7 3 2.7 3 5.8"}],
  "device":[{"tag":"rect","x":6,"y":3,"width":12,"height":18,"rx":1.5},{"tag":"path","d":"M9 7h6M9 10h6M9 13h4"},{"tag":"path","d":"M11 18h2"}],
  "pulse":[{"tag":"path","d":"M3 12h4l2-5 4 10 2-5h6"}],
  "plug":[{"tag":"path","d":"M9 3v5M15 3v5"},{"tag":"path","d":"M7 8h10v4a5 5 0 0 1-10 0z"},{"tag":"path","d":"M12 17v4"}],
  "antenna":[{"tag":"circle","cx":12,"cy":12,"r":1.8},{"tag":"path","d":"M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.6 5.6a9 9 0 0 0 0 12.8M18.4 5.6a9 9 0 0 1 0 12.8"}],
  "gear":[{"tag":"circle","cx":12,"cy":12,"r":3},{"tag":"path","d":"M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"}],
  "download":[{"tag":"path","d":"M12 4v11"},{"tag":"path","d":"m7 10 5 5 5-5"},{"tag":"path","d":"M5 20h14"}],
  "send":[{"tag":"path","d":"M21 3 10 14"},{"tag":"path","d":"m21 3-7 18-4-7-7-4z"}],
  "read":[{"tag":"path","d":"M3 5.5c3-1 6-1 9 1 3-2 6-2 9-1V19c-3-1-6-1-9 1-3-2-6-2-9-1z"},{"tag":"path","d":"M12 6.5V20"}],
  "bookmark":[{"tag":"path","d":"M7 3h10v18l-5-4-5 4z"}],
  "left":[{"tag":"path","d":"m15 5-7 7 7 7"}],
  "right":[{"tag":"path","d":"m9 5 7 7-7 7"}],
  "down":[{"tag":"path","d":"m5 9 7 7 7-7"}],
  "x":[{"tag":"path","d":"M6 6l12 12M18 6 6 18"}],
  "plus":[{"tag":"path","d":"M12 5v14M5 12h14"}],
  "alert":[{"tag":"path","d":"M12 3.5 21.5 20h-19z"},{"tag":"path","d":"M12 10v4.5M12 17.3v.1"}],
  "info":[{"tag":"circle","cx":12,"cy":12,"r":9},{"tag":"path","d":"M12 11v5.5M12 7.8v.1"}],
  "check":[{"tag":"path","d":"m5 12.5 4.5 4.5L19 7"}],
  "lock":[{"tag":"rect","x":5,"y":10.5,"width":14,"height":10,"rx":1.5},{"tag":"path","d":"M8 10.5v-3a4 4 0 0 1 8 0v3"}],
  "sun":[{"tag":"circle","cx":12,"cy":12,"r":4},{"tag":"path","d":"M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"}],
  "moon":[{"tag":"path","d":"M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"}],
  "logout":[{"tag":"path","d":"M14 4h5v16h-5"},{"tag":"path","d":"m10 8-4 4 4 4"},{"tag":"path","d":"M6 12h10"}],
  "filter":[{"tag":"path","d":"M4 5h16l-6 7.5V19l-4 1.5v-8z"}],
  "sort":[{"tag":"path","d":"M7 4v16M4 17l3 3 3-3M17 20V4M14 7l3-3 3 3"}],
  "grid":[{"tag":"rect","x":4,"y":4,"width":6.5,"height":6.5},{"tag":"rect","x":13.5,"y":4,"width":6.5,"height":6.5},{"tag":"rect","x":4,"y":13.5,"width":6.5,"height":6.5},{"tag":"rect","x":13.5,"y":13.5,"width":6.5,"height":6.5}],
  "list":[{"tag":"path","d":"M9 6h11M9 12h11M9 18h11"},{"tag":"path","d":"M4.5 6h.1M4.5 12h.1M4.5 18h.1"}],
  "more":[{"tag":"path","d":"M5 12h.1M12 12h.1M19 12h.1"}],
  "refresh":[{"tag":"path","d":"M20 11a8 8 0 0 0-14.6-4.4L4 8"},{"tag":"path","d":"M4 4v4h4"},{"tag":"path","d":"M4 13a8 8 0 0 0 14.6 4.4L20 16"},{"tag":"path","d":"M20 20v-4h-4"}],
  "shield":[{"tag":"path","d":"M12 3 4.5 6v5.5c0 4.5 3.2 8 7.5 9.5 4.3-1.5 7.5-5 7.5-9.5V6z"},{"tag":"path","d":"m9 12 2.2 2.2L15.5 10"}],
  "key":[{"tag":"circle","cx":8,"cy":15,"r":4},{"tag":"path","d":"m11 12 9-9M17 6l2 2M15 8l2 2"}],
  "trash":[{"tag":"path","d":"M4 7h16M9 7V4h6v3"},{"tag":"path","d":"m6 7 1 13h10l1-13"}],
  "clock":[{"tag":"circle","cx":12,"cy":12,"r":9},{"tag":"path","d":"M12 7v5l3 2"}],
  "offline":[{"tag":"path","d":"m3 3 18 18"},{"tag":"path","d":"M8.3 6.3A6 6 0 0 1 17.5 10H18a4 4 0 0 1 2.6 7"},{"tag":"path","d":"M16 19H7a4.5 4.5 0 0 1-1.7-8.7"}],
  "menu":[{"tag":"path","d":"M4 6h16M4 12h16M4 18h16"}],
  "eye":[{"tag":"path","d":"M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"},{"tag":"circle","cx":12,"cy":12,"r":3}],
  "external":[{"tag":"path","d":"M14 4h6v6M20 4l-9 9"},{"tag":"path","d":"M18 14v6H4V6h6"}],
  "copy":[{"tag":"rect","x":8,"y":8,"width":12,"height":12,"rx":1.5},{"tag":"path","d":"M16 8V4H4v12h4"}],
  "archive":[{"tag":"path","d":"M3 4h18v4H3z"},{"tag":"path","d":"M5 8v12h14V8"},{"tag":"path","d":"M10 12h4"}],
  "pause":[{"tag":"path","d":"M8 5v14M16 5v14"}],
  "play":[{"tag":"path","d":"M7 4.5 19.5 12 7 19.5z"}],
  "user":[{"tag":"circle","cx":12,"cy":8,"r":4},{"tag":"path","d":"M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8"}],
  "type":[{"tag":"path","d":"M3 18 7.5 6 12 18M5 14h5"},{"tag":"path","d":"M14.5 13.5a3 3 0 1 1 6 0V18M20.5 15.5c-3.5-.6-6 .2-6 1.4 0 1.7 4 1.7 6-.4"}],
  "arrows":[{"tag":"path","d":"M4 12h16M8 8l-4 4 4 4M16 8l4 4-4 4"}],
  "expand":[{"tag":"path","d":"M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"}],
  "help":[{"tag":"circle","cx":12,"cy":12,"r":9},{"tag":"path","d":"M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6M12 17.2v.1"}],
  "toc":[{"tag":"path","d":"M4 6h10M4 12h12M4 18h8"},{"tag":"path","d":"M18 6h2M20 12h.1M14 18h6"}],
  "hourglass":[{"tag":"path","d":"M7 3h10M7 21h10"},{"tag":"path","d":"M8 3c0 5 8 5 8 9s-8 4-8 9M16 3c0 5-8 5-8 9"}],
  "drive":[{"tag":"rect","x":3,"y":13,"width":18,"height":7,"rx":1},{"tag":"path","d":"m5 13 2-8h10l2 8"},{"tag":"path","d":"M16.5 16.5h.1"}],
  "tasks":[{"tag":"path","d":"M9 6h11M9 12h11M9 18h11"},{"tag":"path","d":"m3.5 6 1.2 1.2L7 5M3.5 12l1.2 1.2L7 11"},{"tag":"path","d":"M4 18h2"}],
  "wifi":[{"tag":"path","d":"M2 8.5a15 15 0 0 1 20 0M5 12a10 10 0 0 1 14 0M8.5 15.5a5 5 0 0 1 7 0"},{"tag":"path","d":"M12 19h.1"}],
  "qr":[{"tag":"path","d":"M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4z"},{"tag":"path","d":"M14 14h2v2h-2zM18 18h2v2h-2zM14 18h2M18 14h2"}],
  "pin":[{"tag":"path","d":"M9 3h6l-1 6 4 4H6l4-4z"},{"tag":"path","d":"M12 13v8"}],
  "undo":[{"tag":"path","d":"M9 14 4 9l5-5"},{"tag":"path","d":"M4 9h11a5 5 0 0 1 0 10h-3"}],
  "ban":[{"tag":"circle","cx":12,"cy":12,"r":9},{"tag":"path","d":"m5.6 5.6 12.8 12.8"}],
  "box":[{"tag":"path","d":"M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z"},{"tag":"path","d":"m3 7.5 9 4.5 9-4.5M12 12v9"}]
} as const satisfies Record<string, readonly Shape[]>;

export type IconName = keyof typeof ICONS;

/** The mark of the house: an open book with its ribbon (32 grid). */
export const MARK: readonly Shape[] = [{"tag":"path","d":"M5 6h9c1.7 0 2 1 2 2.5V27c0-1.3-.8-2-2-2H5z"},{"tag":"path","d":"M27 6h-9c-1.7 0-2 1-2 2.5V27c0-1.3.8-2 2-2h9z"},{"tag":"path","d":"M21.5 6v8l1.6-1.3 1.6 1.3V6"}];

/** The tail-piece of the colophon (40 by 16 grid). */
export const FLEURON: readonly Shape[] = [{"tag":"path","d":"M2 8h12M26 8h12"},{"tag":"path","d":"M20 13c-3-1-5-4-3-6s4 1 3 3M20 13c3-1 5-4 3-6s-4 1-3 3"},{"tag":"path","d":"M20 4.5v.1"}];

/** Motifs engraved on generated covers (40 grid). */
export const MOTIFS = {
  "spirale":[{"tag":"path","d":"M20 20a2 2 0 1 1 4 0 4 4 0 0 1-8 0 7 7 0 0 1 14 0 10 10 0 0 1-20 0 13 13 0 0 1 26 0"},{"tag":"path","d":"M4 36h32"}],
  "vague":[{"tag":"path","d":"M3 31c7 0 9-11 18-13s11 6 6 9c-3 2-6-1-4-3"},{"tag":"path","d":"M21 18c4-7 13-5 16 2"},{"tag":"path","d":"M3 36h34"},{"tag":"path","d":"M28 31l3-6 3 6z"}],
  "globe":[{"tag":"circle","cx":20,"cy":20,"r":14},{"tag":"ellipse","cx":20,"cy":20,"rx":6,"ry":14},{"tag":"path","d":"M6 20h28M8 13h24M8 27h24"}],
  "lune":[{"tag":"path","d":"M25 6a14 14 0 1 0 9 23A12 12 0 0 1 25 6z"},{"tag":"path","d":"M33 8v4M31 10h4M8 7v3M6.5 8.5h3"}],
  "montre":[{"tag":"circle","cx":20,"cy":23,"r":12},{"tag":"path","d":"M20 23v-7M20 23l5 3M16 6h8M20 6v5"},{"tag":"path","d":"M20 13v.1M30 23h-.1M20 33v.1M10 23h.1"}],
  "etoiles":[{"tag":"path","d":"M28 5a10 10 0 1 0 7 17A9 9 0 0 1 28 5z"},{"tag":"path","d":"m10 10 1.4 3 3 .4-2.2 2 .6 3-2.8-1.5-2.8 1.5.6-3-2.2-2 3-.4z"},{"tag":"path","d":"M6 32c4-3 8-3 12 0s8 3 12 0 6-2 8-1"}],
  "foret":[{"tag":"path","d":"M7 33 13 15l6 18z"},{"tag":"path","d":"M17 33l8-24 8 24z"},{"tag":"path","d":"M3 35h34"},{"tag":"path","d":"M13 33v2M25 33v2"}],
  "phare":[{"tag":"path","d":"m16 35 2.5-21h3L24 35z"},{"tag":"path","d":"M15 14h10M17.5 9h5v5h-5zM20 5v4"},{"tag":"path","d":"m25 10 11-4M25 12.5l11 4"},{"tag":"path","d":"M4 35h32"}],
  "lucioles":[{"tag":"path","d":"M6 34c6-12 14-17 26-20"},{"tag":"path","d":"M15 25c-4-1-6-4-5-7 3 0 5 3 5 7zM22 20c0-4 2-6 5-6 0 3-2 5-5 6z"},{"tag":"circle","cx":30,"cy":27,"r":1.4},{"tag":"circle","cx":9,"cy":10,"r":1.4},{"tag":"circle","cx":33,"cy":8,"r":1.4},{"tag":"circle","cx":18,"cy":9,"r":1}],
  "fleuron":[{"tag":"path","d":"M20 30c-6-2-11-8-7-13s9 2 7 7"},{"tag":"path","d":"M20 30c6-2 11-8 7-13s-9 2-7 7"},{"tag":"path","d":"M20 30v6M14 36h12"},{"tag":"path","d":"M20 12c-2-3-1-6 0-7 1 1 2 4 0 7z"}],
  "obus":[{"tag":"path","d":"M20 4c5 5 6 13 4 22h-8c-2-9-1-17 4-22z"},{"tag":"path","d":"m16 26-4 6h6M24 26l4 6h-6"},{"tag":"circle","cx":20,"cy":15,"r":2.2}],
  "plume":[{"tag":"path","d":"M30 5C18 7 10 17 9 33"},{"tag":"path","d":"M30 5c0 10-7 18-18 20"},{"tag":"path","d":"M14 22l6 1M17 16l6 1M21 11l5 .5"},{"tag":"path","d":"M6 35h10"}]
} as const satisfies Record<string, readonly Shape[]>;

export type MotifName = keyof typeof MOTIFS;
