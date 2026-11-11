# Paquet de diagnostic

`shelfhold diagnostic bundle --out <fichier.zip>` écrit un paquet à joindre à une demande d'aide.
La commande est locale : elle lit la base et les fichiers qu'on lui donne, n'ouvre aucune
connexion et n'écrit que le fichier demandé, en `0600`. Elle n'écrase jamais un fichier existant.
L'interface viendra en M3.

```sh
docker compose logs --no-color shelfhold > /tmp/shelfhold.log
docker compose exec shelfhold shelfhold diagnostic bundle \
  --out /data/diagnostic.zip --log-file /tmp/shelfhold.log
```

`--log-file` est facultatif et se répète. Sans lui, le paquet ne contient aucun journal :
Shelfhold écrit ses journaux sur la sortie standard, jamais dans un fichier (voir
[journaux.md](journaux.md)).

## Ce qui entre

Le paquet est **fermé par défaut** : une liste positive dit ce qui entre, rien d'autre n'y entre.

| Fichier | Contenu |
| --- | --- |
| `manifest.json` | version du format et de Shelfhold, révision du schéma, nombre de lignes de journal gardées et écartées |
| `config.json` | chaque réglage, classé : nombres, interrupteurs et mots fermés tels quels ; `set` ou `unset` pour les secrets, les URL et les fichiers ; `default` ou `custom` pour les volumes ; un compte pour les listes ; la classe de l'adresse d'écoute (`loopback`, `all_interfaces`…) ; l'origine de chaque valeur |
| `status.json` | les codes et les comptes de `shelfhold status`, jamais ses messages |
| `schema.sql` | la structure de la base, sans aucune ligne |
| `content.json` | nombre et taille des fichiers par format ; un fichier **tatoué** n'est cité que par son empreinte et sa taille |
| `sources.json` | sous-sources et séries amont sous `<source-1>`, `<source-2>`… |
| `logs.jsonl` | chaque ligne JSON du journal donné, réduite à ses champs autorisés : événement, niveau, module, horodatage, mots fermés (`outcome`, `code`…), nombres, identifiants internes (ULID) ; un identifiant d'utilisateur devient `<user-N>` |

## Ce qui n'entre jamais

- Aucun fichier de contenu, et en particulier aucun fichier tatoué : un achat tatoué se lit dans
  le foyer, jamais au-delà (ENF-LEG-009, ADR-0033). Un fichier de `/store`, de la bibliothèque,
  d'une racine physique, du miroir de provenance (`.shelfhold/manifest/`), une archive ou un fichier
  dont l'empreinte est celle d'un contenu est **refusé** comme journal.
- Aucun secret : ni clé d'API, ni mot de passe, ni clé du trousseau, ni secret scellé.
- Aucun titre, aucun chemin (un chemin contient un titre), aucune URL de source, aucun domaine,
  aucun nom de sous-source, aucune adresse IP, aucun nom d'hôte, aucune adresse e-mail, aucun
  identifiant d'utilisateur en clair.

Une ligne de journal qui n'est pas un objet JSON (une trace d'erreur en texte, par exemple) est
écartée et comptée. Un champ que la liste ne connaît pas est retiré, pas masqué.

## La dernière barrière

Avant d'écrire, la commande cherche dans chaque fichier du paquet toutes les valeurs que l'instance
garde et qui ne doivent pas sortir : secrets enregistrés et secrets scellés (ouverts pour la
vérification seulement), titres, chemins, URL et hôtes des sources, noms de sous-sources, noms et
identifiants d'utilisateurs, URL configurées, nom de la machine. Elle les cherche en clair,
encodées dans une URL et en base64, et cherche aussi toute adresse e-mail, adresse IP ou URL.

Si l'une d'elles est trouvée, **rien n'est écrit** et la commande sort en erreur (code 1) en
nommant la catégorie et le fichier du paquet, jamais la valeur. C'est un défaut de Shelfhold :
signalez-le, sans le paquet.

Codes de sortie : `0` paquet écrit ; `1` paquet refusé par la dernière barrière ; `2` fichier
existant, dossier absent, sortie demandée dans `/store`, la bibliothèque ou une racine physique, ou
journal refusé.
