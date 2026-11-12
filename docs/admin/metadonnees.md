# Métadonnées : fournisseurs, provenance et verrous

Un **fournisseur de métadonnées** répond à la question « que sait-on de cette Édition ? ». Il ne fournit pas de
fichier : il propose des valeurs (titre, éditeur, date, identifiants, couverture), chacune avec sa provenance.
Shelfhold décide ce qu'il garde. M2 livre le contrat (SDK 1.2, voir
[../plugins/metadata-provider.md](../plugins/metadata-provider.md)) et un fournisseur, **OpenLibrary**.

## OpenLibrary

- Conditions : `terms_url = https://openlibrary.org/developers/api`. Les données sont sous CC0 ; la mention
  « Data from Open Library (openlibrary.org), CC0 1.0 » accompagne chaque valeur reçue.
- Débit : 1 requête par seconde (jeton unique, sans rafale). Les 3 requêtes par seconde que le fournisseur accorde à un
  agent qui donne un contact ne s'ouvrent que si vous réglez `SHELFHOLD_METADATA_CONTACT` (une adresse ou une page de
  contact de votre choix). Plafond : 5000 requêtes par jour. Le solde survit à un redémarrage, et `robots.txt` compte
  comme une requête.
- Agent : `Shelfhold/x.y (+adresse du dépôt)`, avec votre contact s'il est réglé. Aucune adresse n'est écrite par
  défaut, et un plugin ne peut pas la remplacer.
- Cache : obligatoire (RFC 9111). Une couverture est téléchargée par l'hôte et décodée dans le worker de décodage ;
  le fournisseur ne voit jamais d'octets d'image.
- Fournisseur injoignable : une alerte `metadata.provider_down` au plus une fois par heure, rien d'autre ne change.

## Demander ce qu'OpenLibrary sait

```
shelfhold meta enrich 9780306406157     # un ISBN (10 ou 13 chiffres)
shelfhold meta enrich <identifiant-d-édition>
```

Un ISBN désigne une **Édition** (ou un Tome), jamais une Œuvre : ce que le fournisseur sait de l'Œuvre passe par une
relation proposée, pas par un champ. La commande ne remplace **aucun champ verrouillé ni saisi à la main**, et
n'applique **jamais un âge** : un âge proposé est enregistré comme suggestion et ne change aucun classement
([a-classer.md](a-classer.md), `shelfhold rate`).

## Provenance et verrous

Chaque valeur garde son fournisseur, son origine (fournisseur, source, saisie à la main), sa confiance et sa date.
Un champ **verrouillé** n'est jamais remplacé : la proposition est ignorée et signalée.

```
shelfhold meta lock <édition> <champ> [valeur] [--lang <code>] [--on edition]
shelfhold meta unlock <édition> <champ> [--lang <code>] [--on edition]
```

`lock` avec une valeur pose la vôtre (saisie à la main, verrouillée) ; sans valeur, il verrouille celle qui est déjà
enregistrée. `unlock` lève le verrou mais garde la valeur et sa provenance, et une valeur saisie à la main n'est jamais
remplacée par un fournisseur. La route HTTP est `POST /api/v1/editions/{édition}/fields/{champ}/lock` (et `/unlock`).
Voir aussi [proprietaire-des-champs.md](proprietaire-des-champs.md) pour les champs que Shelfhold possède côté Kavita.

## Identifiants externes : ajout seul

Les identifiants externes (ISBN, identifiants de fournisseur) ne sont **jamais écrasés ni supprimés** : la base
refuse toute modification. Si un fournisseur renumérote un identifiant :

```
shelfhold edition renumber-id <édition> <fournisseur> <ancien> <nouveau> [--id-scheme <schéma>]
```

Le nouveau est ajouté et l'ancien marqué remplacé.

## Rattachement automatique, affiché et annulable

Une Édition qui arrive avec l'identifiant d'Œuvre qu'une autre Œuvre porte est rattachée à cette Œuvre. Le geste
est toujours dit (événement `edition.auto_attached`) et annulable :

```
shelfhold edition attachments                       # la liste des rattachements
shelfhold edition detach <rattachement>             # « Séparer » : remet l'état d'avant
shelfhold edition attach-confirm <rattachement>     # plan de retrait d'un rattachement suggéré, puis --confirm <jeton>
shelfhold edition attach-decline <rattachement>
```

Un rattachement qui relèverait l'âge effectif d'une Œuvre n'est jamais automatique : il devient une suggestion que
vous confirmez. Une Édition nouvelle reste **non classée** tant que vous ne l'avez pas classée, donc invisible d'une
étagère famille ([famille.md](famille.md)). Fusionner deux Éditions : `shelfhold edition merge` (plan par défaut,
`--apply` avec son empreinte) ; les pierres tombales sont consultées avant toute fusion ([purge.md](purge.md)).

## Contrôler les projections

`shelfhold meta check` liste les fichiers de projection (`unit.json`, `work.json`, miroir de provenance) qui
divergent de la base, sans rien écrire (code 1 s'il y en a) ; `shelfhold meta rebuild [--unit <id>]` les réécrit.
