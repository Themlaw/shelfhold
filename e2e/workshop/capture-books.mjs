// The readable works of the capture script: public domain texts (the Fables of Jean de La Fontaine,
// 1668), as an EPUB of five chapters and as a book of illustrated plates (pages drawn by the browser
// itself, from the same verses). Nothing here comes from a network.

export const FABLES = [
  {
    title: 'La Cigale et la Fourmi',
    verses: [
      'La Cigale, ayant chanté',
      'Tout l’été,',
      'Se trouva fort dépourvue',
      'Quand la bise fut venue.',
      'Pas un seul petit morceau',
      'De mouche ou de vermisseau.',
      'Elle alla crier famine',
      'Chez la Fourmi sa voisine,',
      'La priant de lui prêter',
      'Quelque grain pour subsister',
      'Jusqu’à la saison nouvelle.',
      '« Je vous paierai, lui dit-elle,',
      'Avant l’août, foi d’animal,',
      'Intérêt et principal. »'
    ]
  },
  {
    title: 'Le Corbeau et le Renard',
    verses: [
      'Maître Corbeau, sur un arbre perché,',
      'Tenait en son bec un fromage.',
      'Maître Renard, par l’odeur alléché,',
      'Lui tint à peu près ce langage :',
      '« Hé ! bonjour, Monsieur du Corbeau.',
      'Que vous êtes joli ! que vous me semblez beau !',
      'Sans mentir, si votre ramage',
      'Se rapporte à votre plumage,',
      'Vous êtes le Phénix des hôtes de ces bois. »',
      'À ces mots, le Corbeau ne se sent pas de joie ;',
      'Et pour montrer sa belle voix,',
      'Il ouvre un large bec, laisse tomber sa proie.'
    ]
  },
  {
    title: 'Le Loup et l’Agneau',
    verses: [
      'La raison du plus fort est toujours la meilleure :',
      'Nous l’allons montrer tout à l’heure.',
      'Un Agneau se désaltérait',
      'Dans le courant d’une onde pure.',
      'Un Loup survient à jeun, qui cherchait aventure,',
      'Et que la faim en ces lieux attirait.',
      '« Qui te rend si hardi de troubler mon breuvage ?',
      'Dit cet animal plein de rage :',
      'Tu seras châtié de ta témérité. »'
    ]
  },
  {
    title: 'Le Lièvre et la Tortue',
    verses: [
      'Rien ne sert de courir ; il faut partir à point :',
      'Le Lièvre et la Tortue en sont un témoignage.',
      '« Gageons, dit celle-ci, que vous n’atteindrez point',
      'Sitôt que moi ce but. — Sitôt ? Êtes-vous sage ?',
      'Repartit l’animal léger :',
      'Ma commère, il vous faut purger',
      'Avec quatre grains d’ellébore. »'
    ]
  },
  {
    title: 'La Laitière et le Pot au lait',
    verses: [
      'Perrette, sur sa tête ayant un Pot au lait',
      'Bien posé sur un coussinet,',
      'Prétendait arriver sans encombre à la ville.',
      'Légère et court vêtue elle allait à grands pas ;',
      'Ayant mis ce jour-là, pour être plus agile,',
      'Cotillon simple et souliers plats.'
    ]
  }
];

const NS = 'http://www.w3.org/1999/xhtml';
const xhtml = (body, head = '') =>
  `<?xml version="1.0" encoding="utf-8"?><html xmlns="${NS}" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>c</title>${head}</head><body>${body}</body></html>`;

const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');

/** The files of the EPUB, by archive name (`OEBPS/...`): one chapter per fable. */
export function epubFiles(title = 'Fables') {
  const chapters = FABLES.map((f, i) =>
    xhtml(
      `<h1>${esc(f.title)}</h1>` +
        `<p>${f.verses.map(esc).join('<br/>')}</p>` +
        // the prose that fills the page: the reader must scroll and turn pages
        `<p>${Array.from({ length: 14 }, () => esc(f.verses.join(' '))).join(' ')}</p>`
    )
  );
  const items = chapters
    .map((_, i) => `<item id="c${i + 1}" href="c${i + 1}.xhtml" media-type="application/xhtml+xml"/>`)
    .join('');
  const spine = chapters.map((_, i) => `<itemref idref="c${i + 1}"/>`).join('');
  const opf =
    `<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="i">` +
    `<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="i">urn:fables</dc:identifier><dc:title>${esc(title)}</dc:title><dc:language>fr</dc:language></metadata>` +
    `<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>${items}</manifest><spine>${spine}</spine></package>`;
  const nav = xhtml(
    `<nav epub:type="toc"><ol>${FABLES.map((f, i) => `<li><a href="c${i + 1}.xhtml">${esc(f.title)}</a></li>`).join('')}</ol></nav>`
  );
  return {
    'OEBPS/content.opf': opf,
    'OEBPS/nav.xhtml': nav,
    ...Object.fromEntries(chapters.map((c, i) => [`OEBPS/c${i + 1}.xhtml`, c]))
  };
}

/** The HTML of one illustrated plate (page `n` of `count`): a ruled frame, a drawn scene, the verses. */
export function plateHtml(n, count, width, height) {
  const fable = FABLES[(n - 1) % FABLES.length];
  const hue = (n * 47) % 360;
  const verses = fable.verses.slice(0, 6).map(esc).join('<br/>');
  return `<!doctype html><meta charset="utf-8"><body style="margin:0;width:${width}px;height:${height}px;background:#f3ecdc;font-family:Georgia,serif;color:#241d14;position:relative;overflow:hidden">
<div style="position:absolute;inset:3%;border:${Math.round(width / 160)}px solid #241d14"></div>
<div style="position:absolute;left:8%;right:8%;top:7%;height:52%;border:3px solid #241d14;background:linear-gradient(180deg,hsl(${hue} 45% 78%),hsl(${(hue + 40) % 360} 40% 88%))">
 <div style="position:absolute;right:12%;top:10%;width:18%;aspect-ratio:1;border-radius:50%;background:hsl(${(hue + 180) % 360} 70% 62%);border:3px solid #241d14"></div>
 <div style="position:absolute;left:0;right:0;bottom:0;height:30%;background:hsl(${(hue + 90) % 360} 30% 40%);border-top:3px solid #241d14"></div>
 <div style="position:absolute;left:18%;bottom:20%;width:14%;height:34%;background:#241d14;border-radius:50% 50% 8% 8%"></div>
</div>
<div style="position:absolute;left:8%;right:8%;top:63%;font-size:${Math.round(width / 28)}px;line-height:1.35">
 <div style="font-variant:small-caps;letter-spacing:.06em;font-size:${Math.round(width / 22)}px;margin-bottom:.4em">${esc(fable.title)}</div>${verses}</div>
<div style="position:absolute;bottom:4%;width:100%;text-align:center;font-size:${Math.round(width / 40)}px">${n} / ${count}</div></body>`;
}
