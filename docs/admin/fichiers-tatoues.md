# Fichiers tatoués : préservés, jamais diffusés hors du foyer

Beaucoup d'EPUB achetés sans verrou technique portent un **tatouage** : une marque qui identifie l'acheteur (nom,
adresse, numéro de commande), dans une entrée de l'archive, dans les métadonnées ou dans le texte. Un achat tatoué se
lit dans le foyer, jamais au-delà. Shelfhold ne retire pas le tatouage et ne le fera jamais (ADR-0033, ENF-LEG-009).

## Ce que Shelfhold fait d'un fichier tatoué

- **Il ne le réécrit pas** : seul l'OPF peut recevoir les champs de série que Shelfhold pose (`calibre:series`,
  `series_index`, `belongs-to-collection`), jamais le contenu.
- **Il ne l'envoie qu'à une destination du foyer.** Chaque destination déclare `egress = foyer` ou `externe` ; le
  planificateur refuse un fichier `watermarked` vers une destination `externe`.
- **Il ne l'inclut dans aucun paquet de diagnostic** : seuls son empreinte et sa taille sont cités
  ([diagnostic.md](diagnostic.md)).
- Il n'entre ni dans une fixture de test, ni dans une demande d'aide.

## Comment un fichier devient « tatoué »

1. **Un achat déclaré.** Déposez le fichier dans le sous-dossier `achats/` du dossier d'import et lancez
   `shelfhold import scan --purchase` ([dossier-import.md](dossier-import.md)). Le fichier est `publisher` (jamais
   réécrit) et marqué `watermarked`. Le passage automatique, lui, laisse `achats/` en place : un achat ne se déclare
   que par votre geste.
2. **Une heuristique.** Des entrées ou des métadonnées typiques d'une copie personnalisée posent le drapeau. C'est une
   **suggestion** : elle ne refuse jamais un fichier, ne dispense jamais un EPUB non fiable du neutraliseur
   ([neutraliseur.md](neutraliseur.md)), et le résultat de l'import la dit toujours.

## Un plugin tiers et `egress`

Un plugin qui n'est pas livré avec Shelfhold ne peut déclarer `egress = foyer` que par votre attestation
(`shelfhold plugin attest <clé> egress foyer`, voir [plugins-tiers.md](plugins-tiers.md)) ; sans elle, c'est `externe`
et un fichier tatoué n'y part pas.

## Ce que Shelfhold ne promet pas

Il ne détecte pas tous les tatouages (un texte modifié, une marque dans une image) et ne dit pas qu'un fichier est
« propre ». Ne partagez pas un achat : le drapeau protège contre l'envoi par Shelfhold, pas contre un partage que vous
feriez vous-même.
