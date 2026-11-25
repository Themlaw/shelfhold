// The scenes of the generated covers (docs/design/papier-et-encre.md, « Couvertures »), drawn for
// this project from the Paper and ink direction: a manga, webtoon or comic cover without an image
// gets one of its family's scenes, chosen from the series. Data, never an HTML string: the
// elements are drawn by CoverArt.svelte. Classes: k ink fill, ks ink stroke, kf fine ink stroke,
// p paper fill, ps paper stroke, a b c d the scene's colours, sk an inked outline.
export type ArtNode =
  | { tag: "path"; d: string; cls?: string }
  | { tag: "circle"; cx: number; cy: number; r: number; cls?: string }
  | {
      tag: "rect";
      x?: number;
      y?: number;
      width: number;
      height: number;
      cls?: string;
    }
  | { tag: "g"; cls?: string; children: ArtNode[] };

export interface Scene {
  name: string;
  /** the box of the drawing: a whole cover (200x300), or a comic panel (200x190) */
  viewBox: string;
  nodes: ArtNode[];
}

export const SCENES: Record<"manga" | "webtoon" | "comic", readonly Scene[]> = {
  manga: [
    {
      name: "hokusai",
      viewBox: "0 0 200 300",
      nodes: [
        { tag: "circle", cls: "a", cx: 40, cy: 60, r: 19 },
        { tag: "path", cls: "kf", d: "M8 22h118M8 30h92M70 44h50M76 52h34" },
        {
          tag: "path",
          cls: "k",
          d: "M0 300V175C10 140 40 108 80 104c32-3 56 16 56 38 0 16-16 24-28 16-10-7-6-20 4-20-8-10-30-10-42 8-14 22-4 68 30 98 18 16 36 28 50 36v20z",
        },
        {
          tag: "path",
          cls: "ps",
          d: "M58 112c4-6 10-8 15-6M82 106c4-5 9-6 14-4M104 109c4-3 9-3 12 0M20 200c14-20 30-30 44-32M14 240c18-22 36-34 54-36M30 282c16-14 34-22 50-22",
        },
        { tag: "path", cls: "ks", d: "M104 236l22-34 22 34z" },
        { tag: "path", cls: "k", d: "M118 214l8-12 8 12-4-2-4 4-4-4z" },
      ],
    },
    {
      name: "lucioles",
      viewBox: "0 0 200 300",
      nodes: [
        {
          tag: "path",
          cls: "k",
          d: "M0 0h200v176c-30-16-62-16-92-2s-70 18-108 6z",
        },
        { tag: "circle", cls: "p", cx: 50, cy: 56, r: 20 },
        { tag: "circle", cls: "k", cx: 59, cy: 50, r: 17 },
        { tag: "circle", cls: "a", cx: 40, cy: 112, r: 4 },
        { tag: "circle", cls: "a", cx: 84, cy: 138, r: 3 },
        { tag: "circle", cls: "a", cx: 112, cy: 94, r: 5 },
        { tag: "circle", cls: "a", cx: 128, cy: 150, r: 3 },
        { tag: "circle", cls: "a", cx: 22, cy: 158, r: 3 },
        { tag: "circle", cls: "a", cx: 96, cy: 62, r: 2.5 },
        { tag: "circle", cls: "a sk", cx: 66, cy: 212, r: 4.5 },
        { tag: "circle", cls: "a sk", cx: 104, cy: 236, r: 4 },
        { tag: "circle", cls: "a sk", cx: 30, cy: 226, r: 3.5 },
        {
          tag: "path",
          cls: "ks",
          d: "M10 300c2-30 6-50 14-70M22 300c0-24-4-44-12-60M40 300c2-20 8-36 18-48M70 300c-2-28-8-46-18-58M96 300c2-20 6-34 14-44M120 300c-2-22-6-38-14-50M140 300c2-18 8-30 16-38",
        },
      ],
    },
    {
      name: "heron",
      viewBox: "0 0 200 300",
      nodes: [
        { tag: "circle", cls: "a", cx: 80, cy: 120, r: 58 },
        { tag: "path", cls: "kf", d: "M8 30h60M8 38h40" },
        {
          tag: "path",
          cls: "k",
          d: "M56 150c8-18 34-24 52-14 10 6 12 18 4 24-14 10-40 10-56-10zM56 150l-20 14 24-4z",
        },
        {
          tag: "path",
          cls: "ks ks--thick",
          d: "M104 140c10-14 6-28-6-34-10-6-8-22 4-26",
        },
        {
          tag: "path",
          cls: "k",
          d: "M98 76c6-5 14-3 17 3l28 5-28 4c-5 4-13 3-17-3z",
        },
        {
          tag: "path",
          cls: "ks",
          d: "M86 164v70M80 240l6-6 6 6M94 162l10 18-12 10M30 252h110M50 264h70M66 276h38M18 300c4-40 8-70 6-100M30 300c2-30 6-50 14-64",
        },
      ],
    },
    {
      name: "abysses",
      viewBox: "0 0 200 300",
      nodes: [
        {
          tag: "path",
          cls: "ks",
          d: "M0 84c16-6 32-6 48 0s32 6 48 0M0 100c20-8 40-8 60 0s40 8 60 0 40-8 80 0",
        },
        {
          tag: "path",
          cls: "k",
          d: "M0 120c20-8 40-8 60 0s40 8 60 0 40-8 80 0v180H0z",
        },
        {
          tag: "path",
          cls: "p",
          d: "M18 204c0-13 30-22 64-22s66 9 66 22-32 22-66 22-64-9-64-22zM72 184l6-18h24l4 18zM146 202l16-3-16 8z",
        },
        { tag: "circle", cls: "a", cx: 52, cy: 204, r: 4 },
        { tag: "circle", cls: "a", cx: 72, cy: 204, r: 4 },
        { tag: "circle", cls: "a", cx: 92, cy: 204, r: 4 },
        { tag: "circle", cls: "a", cx: 112, cy: 204, r: 4 },
        { tag: "circle", cls: "ps", cx: 112, cy: 160, r: 5 },
        { tag: "circle", cls: "ps", cx: 122, cy: 142, r: 3 },
        { tag: "circle", cls: "ps", cx: 106, cy: 132, r: 4 },
        { tag: "circle", cls: "ps", cx: 40, cy: 252, r: 4 },
        {
          tag: "path",
          cls: "ps",
          d: "M20 300c-6-20 6-30 0-50M40 300c6-24-6-34 2-56M130 300c-6-18 6-28-2-44",
        },
      ],
    },
  ],
  webtoon: [
    {
      name: "phare",
      viewBox: "0 0 200 300",
      nodes: [
        { tag: "rect", cls: "a", width: 200, height: 300 },
        {
          tag: "g",
          cls: "p",
          children: [
            { tag: "circle", cx: 20, cy: 30, r: 1.6 },
            { tag: "circle", cx: 60, cy: 16, r: 1.2 },
            { tag: "circle", cx: 160, cy: 24, r: 1.8 },
            { tag: "circle", cx: 184, cy: 62, r: 1.2 },
            { tag: "circle", cx: 34, cy: 88, r: 1.2 },
            { tag: "circle", cx: 176, cy: 112, r: 1.5 },
          ],
        },
        { tag: "path", cls: "c beam", d: "M112 94 0 40v60zM128 94l72-44v70z" },
        { tag: "path", cls: "p", d: "M108 100h24l8 120h-40z" },
        {
          tag: "path",
          cls: "d",
          d: "M106 128h28l2 18h-32zM102 168h36l2 18h-40z",
        },
        { tag: "rect", cls: "c", x: 110, y: 84, width: 20, height: 16 },
        { tag: "path", cls: "k", d: "M106 84l14-12 14 12z" },
        {
          tag: "path",
          cls: "k",
          d: "M66 236c12-22 32-26 52-22s40 6 62 22v64H66z",
        },
        {
          tag: "path",
          cls: "b",
          d: "M0 226c20-8 40-8 60 0s40 8 60 0 40-8 80 0v74H0z",
        },
        {
          tag: "path",
          cls: "ps ps--soft",
          d: "M0 248c20-6 40-6 60 0s40 6 60 0 40-6 80 0",
        },
      ],
    },
    {
      name: "marees",
      viewBox: "0 0 200 300",
      nodes: [
        { tag: "rect", cls: "a", width: 200, height: 300 },
        { tag: "circle", cls: "c", cx: 100, cy: 168, r: 50 },
        { tag: "path", cls: "b", d: "M0 168h200v132H0z" },
        {
          tag: "path",
          cls: "c",
          d: "M66 182h68v5H66zM76 196h48v4H76zM86 208h28v3H86zM93 218h14v3H93z",
        },
        {
          tag: "path",
          cls: "ks",
          d: "M36 72l8 6 8-6M58 56l6 5 6-5M140 84l7 6 7-6",
        },
        {
          tag: "path",
          cls: "k",
          d: "M0 196h124v6H0zM84 196v-22c0-4 2-6 5-6s5 2 5 6v22zM98 196v-20c0-4 2-6 5-6s5 2 5 6v20z",
        },
        { tag: "circle", cls: "k", cx: 89, cy: 162, r: 5 },
        { tag: "circle", cls: "k", cx: 103, cy: 164, r: 5 },
        {
          tag: "path",
          cls: "ks",
          d: "M10 202v42M40 202v42M70 202v42M104 202v42",
        },
      ],
    },
  ],
  comic: [
    {
      name: "nemo",
      viewBox: "0 0 200 190",
      nodes: [
        { tag: "rect", cls: "a", width: 200, height: 190 },
        {
          tag: "g",
          cls: "p",
          children: [
            { tag: "circle", cx: 20, cy: 24, r: 1.6 },
            { tag: "circle", cx: 64, cy: 14, r: 1.2 },
            { tag: "circle", cx: 100, cy: 40, r: 1.4 },
            { tag: "circle", cx: 186, cy: 96, r: 1.4 },
            { tag: "circle", cx: 30, cy: 70, r: 1.2 },
          ],
        },
        { tag: "circle", cls: "c sk", cx: 152, cy: 42, r: 22 },
        {
          tag: "path",
          cls: "k",
          d: "M0 190v-36l20-14 20 14v-10h16v20l24-16 26 18v-8h14v18l22-14 24 16 34-20v32z",
        },
        {
          tag: "path",
          cls: "ks",
          d: "M60 110c-4 20-10 40-20 60M76 110c0 22-4 42-8 62M120 110c4 20 10 40 18 60M132 110c6 18 14 36 26 54",
        },
        { tag: "path", cls: "b sk", d: "M56 92h82v18H56zM48 68h12v46H48z" },
        { tag: "path", cls: "p sk", d: "M64 86c10-8 52-8 68 0v8H64z" },
        { tag: "circle", cls: "p sk", cx: 72, cy: 80, r: 7 },
        { tag: "path", cls: "p sk", d: "M66 76c2-8 12-10 16-2l6-2-4 6z" },
      ],
    },
    {
      name: "vieuxbois",
      viewBox: "0 0 200 190",
      nodes: [
        { tag: "rect", cls: "a", width: 200, height: 190 },
        {
          tag: "path",
          cls: "p",
          d: "M24 46c0-8 14-10 18-4 4-8 20-6 20 4 8 0 8 10 0 10H26c-6 0-8-10-2-10zM132 30c0-6 10-8 13-3 3-6 15-4 15 3 6 0 6 8 0 8h-26c-5 0-6-8-2-8z",
        },
        { tag: "path", cls: "b sk", d: "M0 150c40-24 100-30 200-10v50H0z" },
        {
          tag: "path",
          cls: "c sk",
          d: "M60 190c20-20 50-34 90-40l10 4c-36 8-60 20-74 36z",
        },
        { tag: "path", cls: "k", d: "M150 142v-42h6v42z" },
        { tag: "circle", cls: "b sk", cx: 153, cy: 86, r: 24 },
        {
          tag: "path",
          cls: "k",
          d: "M74 140l6-40h14l6 40zM79 82h16l-2-16H81z",
        },
        { tag: "circle", cls: "p sk", cx: 87, cy: 90, r: 8 },
        {
          tag: "path",
          cls: "ks",
          d: "M82 140l-6 22M92 140l8 22M94 108l14 14v26",
        },
      ],
    },
    {
      name: "moritz",
      viewBox: "0 0 200 190",
      nodes: [
        { tag: "rect", cls: "a", width: 200, height: 190 },
        { tag: "path", cls: "b sk", d: "M0 112h200v78H0z" },
        {
          tag: "path",
          cls: "kf",
          d: "M0 132h200M0 152h200M0 172h200M30 112v20M80 112v20M130 112v20M180 112v20M55 132v20M105 132v20M155 132v20M30 152v20M80 152v20M130 152v20M180 152v20",
        },
        { tag: "circle", cls: "p sk", cx: 64, cy: 102, r: 18 },
        {
          tag: "path",
          cls: "k",
          d: "M46 98c0-14 10-20 18-20s18 6 18 20c-6-6-12-8-18-8s-12 2-18 8z",
        },
        { tag: "circle", cls: "p sk", cx: 134, cy: 102, r: 18 },
        {
          tag: "path",
          cls: "c sk",
          d: "M118 96c2-10 8-18 16-18s14 8 16 18l-8-6-8 8-8-8z",
        },
        {
          tag: "g",
          cls: "k",
          children: [
            { tag: "circle", cx: 58, cy: 104, r: 2.3 },
            { tag: "circle", cx: 70, cy: 104, r: 2.3 },
            { tag: "circle", cx: 128, cy: 104, r: 2.3 },
            { tag: "circle", cx: 140, cy: 104, r: 2.3 },
          ],
        },
        {
          tag: "path",
          cls: "p sk",
          d: "M42 110h12v8H42zM74 110h12v8H74zM112 110h12v8h-12zM144 110h12v8h-12zM172 112c-2-12 6-20 14-18l6-6 2 8c6 4 6 12 0 16z",
        },
      ],
    },
    {
      name: "lievre",
      viewBox: "0 0 200 190",
      nodes: [
        { tag: "rect", cls: "a", width: 200, height: 190 },
        {
          tag: "g",
          cls: "c",
          children: [
            { tag: "circle", cx: 24, cy: 74, r: 8 },
            { tag: "circle", cx: 50, cy: 48, r: 9 },
            { tag: "circle", cx: 82, cy: 32, r: 10 },
            { tag: "circle", cx: 116, cy: 28, r: 11 },
            { tag: "circle", cx: 148, cy: 34, r: 10 },
            { tag: "circle", cx: 174, cy: 50, r: 9 },
            { tag: "circle", cx: 190, cy: 76, r: 7 },
          ],
        },
        {
          tag: "g",
          cls: "a",
          children: [
            { tag: "circle", cx: 28, cy: 70, r: 7 },
            { tag: "circle", cx: 55, cy: 45, r: 7 },
            { tag: "circle", cx: 88, cy: 30, r: 6 },
            { tag: "circle", cx: 143, cy: 31, r: 7 },
            { tag: "circle", cx: 169, cy: 46, r: 8 },
            { tag: "circle", cx: 186, cy: 72, r: 6 },
          ],
        },
        { tag: "path", cls: "k", d: "M0 190v-36c40-22 120-26 200-6v42z" },
        {
          tag: "path",
          cls: "c",
          d: "M88 154c-6-12-2-28 12-32l-6-26c-2-8 6-10 8-2l6 24 4-26c2-8 10-6 8 2l-4 30c10 6 14 18 10 30z",
        },
        { tag: "circle", cls: "k", cx: 108, cy: 130, r: 1.8 },
      ],
    },
  ],
};
