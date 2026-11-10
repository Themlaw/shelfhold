# Le neutraliseur de XHTML : `xhtml-neutralizer@v1`

Frontière de confiance du **corps** des fichiers (EF-BIB-051, ENF-SEC-015, ADR-0047). Un EPUB dont la source est `untrusted` (service connecté qui récupère du web, flux OPDS de tiers, dossier d'import non qualifié) n'est jamais exporté tel quel : la destination reçoit un **dérivé**, produit par la fonction décrite ici. Le lecteur de livres de Kavita injecte le XHTML sans assainissement et garde ses jetons dans `localStorage` ; la défense ne repose donc jamais sur la destination.

## Ce qu'est la fonction

- Une seule fonction, `neutralize(data, kind)` (`infrastructure/content_trust/xhtml_neutralizer.py`), avec `kind` parmi `xhtml`, `svg` et `css`. Elle est **pure et déterministe** : mêmes octets en entrée, mêmes octets en sortie. Aucune horloge, aucun réseau, aucun état global.
- Elle est **versionnée** (`NEUTRALIZER_VERSION`, `derived_by = xhtml-neutralizer@v1`). Corriger un contournement, c'est publier `@v2` : la version fait partie de l'identité du dérivé, ce qui permet de tout régénérer (migration explicite, depuis l'original conservé dans `/store`, `derived_chain` tenue ; lot L03).
- `epub_scan.neutralize_epub(source, out)` lit l'archive et écrit le dérivé. Il renvoie `derived_by`, `derived_hash`, `derived_hash_by` (`content-hash@2`) et le même hash calculé sur l'original (égaux si rien n'a changé).
- Aucun paramètre ne dit « cette source est de confiance » : un tatouage trouvé par heuristique (EF-BIB-041) ne dispense jamais du neutraliseur. Seule l'attestation d'achat (`publisher`) en dispense, et ce choix se fait en amont, dans le pipeline (L03), jamais ici.
- nh3 n'est appelé sur du XHTML d'EPUB **que** par ce module (contrat `epub-derivative-single-producer`).

## Comment elle travaille

1. Le document est lu par `safe_parse_xml` (ni DTD, ni entité, plafonds de taille, de profondeur et de nombre d'éléments). Un `DOCTYPE` XHTML simple, sans sous-ensemble interne, est retiré et ses entités HTML nommées (`&nbsp;`) deviennent numériques ; tout autre DTD (entité externe, XXE, billion laughs, entité paramètre) est refusé.
2. Le résultat n'est pas l'entrée amputée : c'est un **nouveau document**, écrit depuis l'arbre, élément par élément, selon une liste blanche. Commentaires, instructions de traitement et sections CDATA ne sont jamais recopiés (un lecteur qui lit la page comme du HTML les voit autrement qu'un analyseur XML : `<![CDATA[ > <script>…` cache un script).
3. Un élément hors liste blanche est **déballé** (son texte reste) ; un élément dangereux est **supprimé avec son sous-arbre** (`script`, `iframe`, `object`, `embed`, `form` et ses contrôles, `base`, `foreignObject`, `animate*`, `set`, `video`, `audio`, `math`, `noscript`, `template`…), quel que soit son espace de noms et sa casse (`SCRIPT`, `x:script`, `svg:script`).
4. Les attributs sont filtrés par liste blanche, après une règle générale : tout `on*` est supprimé. Les URL sont jugées sur une copie « aplatie » (sans caractère de contrôle, d'espace ou invisible, en minuscules, `\` lu comme `/`), comme le fait un navigateur, quelle que soit la façon dont les entités l'ont écrite.
   - sont conservés : les références **relatives** au paquet ; sur les images, `data:image/{png,jpeg,gif,webp,avif};base64` ;
   - sont supprimés : `javascript:`, `vbscript:`, `data:` actif, tout autre schéma, `//hôte`, les chemins enracinés (`/…`) ;
   - **décision (revue de la PR #117)** : dans un EPUB `untrusted`, un `a href` vers `http`, `https` ou `mailto` **perd son `href`** et garde son texte ; seuls les liens internes relatifs restent. Le lecteur peut être un enfant (ENF-SEC-015) : aucun lien sortant ne part d'un contenu non fiable ;
   - `<link>` n'est gardé que pour `rel="stylesheet"` vers un fichier du paquet ; `<meta http-equiv>` disparaît ; `use` et `image` SVG ne gardent qu'un fragment ou un fichier du paquet.
5. Les feuilles de style (`<style>`, attribut `style`, fichiers `.css`) sont coupées **règle par règle** : une règle ne survit pas avec `@import`, échappement `\` hors chaîne, `expression()`, `behavior`, `-moz-binding`, `image-set()` ou tout `url()` qui n'est pas relatif. Une feuille propre est recopiée telle quelle.
6. **nh3 est la seconde barrière.** Le corps écrit est passé à nh3 avec la même liste blanche ; les deux sont relus comme suites de balises ouvrantes et d'attributs. Si nh3 aurait retiré quoi que ce soit, l'analyseur HTML et la marche de l'arbre ne voient pas la même page : le fichier est refusé (`invariant`) au lieu d'être exporté.

## Ce qu'elle ne touche pas

- L'**OPF** (tout `.opf`) et **`META-INF/`** ne sont jamais réécrits ; le module ne peut d'ailleurs pas atteindre l'écrivain d'OPF (contrat `opf-sole-writer`). `mimetype` reste en tête et non compressé.
- **Liste blanche positive.** Aucune entrée n'est reconnue en cherchant des balises dans ses octets (un remplissage de 10 Kio, un préfixe d'espace de noms, l'UTF-16 ou un `media-type` avec `; charset=` suffisent à tromper un reniflage). Chaque entrée de l'archive doit tomber dans **exactement une** catégorie, sinon l'EPUB est refusé (`unexpected_entry`) :
  1. `mimetype`, `META-INF/*` et l'OPF : recopiés tels quels, jamais réécrits ; mais `container.xml` et chaque OPF sont **lus en entier** : toute instruction de traitement autre que la déclaration XML, tout `href`, `src`, `refines` ou `xml:base` non relatif (schéma lu sur la valeur brute, racine et `..` sur la valeur décodée : `Part%201%3A%20Intro.xhtml` passe) ou qui sort de la racine du paquet, tout élément XHTML ou SVG, refusent l'EPUB (`remote_reference`, `processing_instruction`, `page_in_xml`). Le `media-type` est coupé au `;` et mis en minuscules ; deux items qui déclarent des types différents pour un même chemin refusent l'EPUB.
  2. XHTML, SVG et CSS : toujours neutralisés, que l'extension (`.xhtml`, `.xht`, `.html`, `.htm`, `.svg`, `.css`) ou le type déclaré les désigne. Si les deux disent des choses différentes (`.png` déclaré `text/html`), l'EPUB est refusé (`type_mismatch`).
  3. NCX et XML (déclarés XML, ou nommés `.xml` ou `.ncx`) : lus en entier par `safe_parse_xml` ; le premier élément d'un espace de noms XHTML ou SVG (ou `html`, `svg`, `script`…), ou toute instruction de traitement, refuse l'EPUB ; sinon recopiés tels quels.
  4. Binaires : images png, jpeg, gif, webp et polices ttf, otf, woff, woff2, recopiés **si et seulement si** leurs premiers octets sont les octets magiques de leur extension (`magic_mismatch` sinon). Aucun reniflage de balises dans une police : une police OpenType-SVG est acceptée, un lecteur ne l'exécute pas comme une page.
  5. Tout le reste est refusé : `.xsl`, `.xslt`, `.js`, `.dat`, `.svgz`, audio et vidéo (refusés tant qu'aucune destination audio n'existe, EF-SRC-012), entrées sans extension connue et sans type de page déclaré. Une entrée absente du manifeste est classée par son extension, avec les mêmes règles.
- Une instruction de traitement `xml-stylesheet` dans une page ou un SVG refuse l'EPUB (`xml_stylesheet`, cherchée dans les octets, UTF-16 et UTF-32 compris) : l'analyseur la perd sans bruit et un lecteur pourrait exécuter la transformation.
- L'archive est lue par `safe_open_archive` (`../`, chemins absolus, liens symboliques, doublons, bombes de décompression, plafonds) et tout XML par `safe_parse_xml`. Un refus porte le code stable `content.untrusted_entry`.

## Corpus d'EPUB piégés

`sdk/testing/corpus/trapped_epubs/` (MIT, dans le kit de conformité) fabrique les EPUB par du code, sans binaire versionné : au moins un cas par vecteur (scripts de toute forme, gestionnaires, schémas `javascript:`/`vbscript:`/`data:` obfusqués, cadres, formulaires, `base`, `meta refresh`, `link` distant, SVG, feuilles de style, XXE, entités, archives hostiles, fichiers déguisés) et plus de 200 **mutations** générées (casse, encodage par entités, espaces et retours chariot insérés). `find_active_patterns` est un détecteur **indépendant** de la fonction. `run_trapped_epub_corpus(derive)` tient n'importe quel neutraliseur contre le corpus. Une propriété hypothesis vérifie que, quelle que soit l'entrée, aucune sortie ne contient un motif actif et que la sortie est un point fixe.

## Limites connues, à relire

- Un fichier XHTML mal formé refuse tout l'EPUB (échec fermé, pas de réparation).
- Un EPUB réel avec des fichiers hors liste (`.txt`, `.plist`, `.DS_Store`, audio) est refusé en entier : c'est le prix de la liste blanche positive.
- Une référence relative qui remonte au-delà de la racine du paquet (`../../…`) n'est pas bornée ici : la fonction ne connaît pas le chemin de l'entrée.
- Le MathML est supprimé ; l'audio et la vidéo aussi.
- `content-hash@2` (hash d'un EPUB sans son OPF) est défini dans `epub_scan.py` ; le lot qui fournit le hash du contenu EPUB (L09) doit l'adopter tel quel ou changer la version.
