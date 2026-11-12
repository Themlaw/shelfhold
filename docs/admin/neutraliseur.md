# Le neutraliseur : ce qui arrive d'un EPUB non fiable

Un EPUB qui vient du web (une source native, lncrawl) ou du dossier d'import peut contenir
du code actif que le lecteur de Kavita exécuterait. Shelfhold le traite donc comme **non fiable** (`untrusted`) et
n'exporte jamais l'original : il exporte un **dérivé neutralisé** (EF-BIB-051, ENF-SEC-015, ADR-0047).

## Ce que le neutraliseur retire

`xhtml-neutralizer@v1` retire des pages XHTML les scripts, les gestionnaires d'événements (`on…`), les adresses
`javascript:` et `data:text/html`, les cadres, objets, éléments `embed` et formulaires, et toute ressource ou
`@import` distant. Il neutralise aussi les SVG (`<script>`, `foreignObject`). La liste de ce qui reste est une liste
blanche, pas une liste noire. Le résultat est déterministe : le même original donne le même dérivé.

Un EPUB que le neutraliseur ne sait pas rendre sûr est **refusé** : ni dérivé, ni original rangé, ni export. L'Unité
passe `quarantined` avec la raison (`content.untrusted_entry`) ; elle sort de cet état quand un autre fichier de la
même Unité arrive d'une source qui passe. Un fichier inconnu de `META-INF/`, un `encryption.xml` ou un élément du
manifeste qui pointe dans `META-INF/` refusent l'EPUB.

## Où vont l'original et le dérivé

| Fichier | Où | Qui le lit |
|---|---|---|
| Original | `/store/untrusted/…` | personne en dehors de Shelfhold : il n'est jamais exporté ni servi |
| Dérivé neutralisé | `/library` | Kavita et toute destination : c'est le seul fichier qui sort |

Sans `/store`, Shelfhold ne garde que le dérivé et le dit (`content.original_not_kept`) : **configurez `/store`** pour
une instance qui active une source non fiable. Un fichier d'un dossier d'import reste alors dans le dossier d'import.

## Quand le neutraliseur change

Une mise à jour de sécurité du neutraliseur change sa version. Les dérivés rangés ne sont pas recalculés d'office :

```
shelfhold content reneutralize                       # aperçu, rien n'est écrit, donne une empreinte
shelfhold content reneutralize --confirm <empreinte> # régénère exactement ce qui a été montré
```

La régénération repart de l'original de `/store` s'il se vérifie, sinon du dérivé rangé. L'ancien fichier n'est
remplacé qu'après vérification du nouveau ; rien n'est supprimé d'abord, et un échec garde l'ancien.

## Ce qui échappe au neutraliseur

- Un fichier **`publisher`** : un achat déclaré, ou une source dont **vous** avez attesté `content_trust`
  ([plugins-tiers.md](plugins-tiers.md)). Il part tel quel, hors des champs de série de l'OPF.
- Un **PDF** : le neutraliseur ne traite que l'EPUB. Un PDF non fiable est rangé tel quel et **n'est pas exporté**
  (« À ranger », raison `content.untrusted_pdf`), parce qu'un PDF peut porter du JavaScript. Un PDF d'achat déclaré
  est exporté tel quel.
- Un **CBZ** : les entrées doivent être de vraies images (reconnues par leurs octets, jamais par l'extension). Un SVG
  d'un CBZ non fiable est rastérisé dans le worker de décodage (sans moteur de script, sans réseau) en un dérivé déclaré ;
  tout autre format est refusé.

## Adoption

`shelfhold adopt` dresse la liste des EPUB déjà présents dont une page contient du code actif. Ils sont adoptés tels
quels : rien n'est réécrit dans une bibliothèque existante.
