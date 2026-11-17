# Destinations : où Shelfhold range les livres

Une **destination** est l'endroit où Shelfhold pose les fichiers qu'il range : une bibliothèque
Kavita, un dossier que lit une application de lecture, la boîte de dépôt d'un importeur. Depuis
M4, une étagère peut être liée à **plusieurs destinations à la fois** (le « fan-out ») : chaque
liaison reçoit sa propre copie, rendue pour la destination qui la reçoit, jamais copiée d'une
destination à l'autre.

Cette page dit quelles destinations existent, comment on les crée, comment on les retire. La
synchronisation de la progression a sa page : [sync.md](sync.md). Le côté famille (un compte
Kavita par lecteur) est dans [famille.md](famille.md).

## Les destinations qui existent

| Destination | Ce que c'est | Création |
|---|---|---|
| **Kavita** | la bibliothèque où se fait la lecture ; Shelfhold écrit dans le dossier que Kavita scanne et l'invite à analyser | `shelfhold target add kavita` (ci-dessous) |
| **dossier natif** | un dossier à la disposition locale de Mihon, ou que lit la source locale de Suwayomi ; aucun serveur, aucune clé | `POST /api/v1/targets/native-folder` |
| **Komga** | une bibliothèque Komga ; le cloisonnement par bibliothèque est prouvé par l'audit des comptes | `shelfhold target add komga` (plus bas) |
| **CWA** | la boîte de dépôt de Calibre-Web Automated : un EPUB déposé, l'importeur le prend | `shelfhold target add cwa` (plus bas) |

Le détail de chaque contrat (routes appelées, dépôt atomique, refus) est dans
[komga-routes.md](../destinations/komga-routes.md) et
[cwa-drop-box.md](../destinations/cwa-drop-box.md).

### Kavita

```sh
shelfhold target add kavita --url https://kavita.example.lan --user shelfhold \
    --password-file kavita.password --accept-admin-risk
```

La clé échangée est celle d'un compte Kavita **administrateur** dédié : la commande s'arrête sur
cet avertissement tant que `--accept-admin-risk` n'est pas là. Le mot de passe est lu dans un
fichier (ou sur l'entrée standard), jamais sur la ligne de commande. La clé expire ; Shelfhold
vous prévient (`target.key_expiring`) et `shelfhold target rotate-key <cible> --password-file …`
la renouvelle. `--path-map` déclare comment les dossiers de Kavita se voient côté Shelfhold (sans
carte, le dossier commun est le parent du dossier média de Kavita). Le même ajout se fait par
`POST /api/v1/targets` depuis une session d'administrateur récente.

### Dossier natif

Un dossier natif se déclare par `POST /api/v1/targets/native-folder` (une session
d'administrateur récente ; aucun écran ne l'écrit encore) : c'est une action réservée, car cela désigne des dossiers écrits. Une
racine qui chevauche le dossier de la source locale de Suwayomi est refusée
(`target.root_overlaps_local_source`) : Shelfhold ne détruit pas ce qu'une autre application
gère. Il n'y a ni serveur ni clé : la destination est l'arbre de dossiers lui-même, à la
disposition locale de Mihon.

### Komga

```sh
shelfhold target add komga --url http://komga.example.lan:25600 --password-file komga.key
```

Le fichier (ou `-` pour l'entrée standard, jamais un argument) contient la **clé d'API d'un compte
administrateur dédié** de Komga (le scan et `empty-trash` exigent ce rôle) ; `--api-key-file` est un
synonyme de `--password-file`. Shelfhold montre la clé une fois à Komga (`/actuator/info`, route
d'administrateur : sa réponse prouve le rôle), la **scelle** dans la ligne de la destination et ne la
relit plus jamais : ni l'API, ni l'export intégral, ni un journal ne la portent. Une clé refusée ou une
adresse privée que le réglage réseau ne déclare pas n'enregistre rien (`target.exchange_failed`).
Options : `--name` (défaut `komga`) et `--path-map`. Relancer la commande avec la **même** clé répond
« déjà présente » sans rien écrire ; avec une **nouvelle** clé, Shelfhold la sonde, la scelle à la place de
l'ancienne et le journal le dit (`target.rotate_komga_key`) : c'est la rotation. Changer la carte des
chemins d'une cible existante par cette porte est journalisé comme `config.path_map`. Même ajout par
`POST /api/v1/targets/komga` (`name`, `url`, `api_key`, `path_map`) : action réservée
(`target.add_komga`), session d'administrateur récente, jamais une clé d'API, un membre ou un enfant.
Tant que l'audit des comptes n'est pas appelé en production, une étagère sous 18+ n'est pas liée à Komga
([komga-routes.md](../destinations/komga-routes.md)).

### CWA

```sh
shelfhold target add cwa --name depot \
    --path-map '[{"local": "/library/depot", "remote": "/cwa-ingest", "mode": "rw"}]'
```

C'est le dossier que Calibre-Web Automated surveille : aucun secret, des dossiers en écriture seulement,
jamais la source locale de Suwayomi. Même ajout par `POST /api/v1/targets/cwa` (`name`, `path_map`) :
action réservée comme le dossier natif (`config.path_map`), journalisé (`target.add_cwa`). Une étagère
se lie à une boîte de dépôt avec le transport `dropbox`.

## Lier une étagère : le fan-out

`POST /api/v1/shelves/{id}/destinations` (action réservée : session d'administrateur récente ou
console de l'hôte) lie une étagère à une cible, avec un sous-dossier (`root_subpath`) et un
transport. Une étagère peut être liée **plusieurs fois** : une liaison par dossier et par serveur.
Deux liaisons peuvent partager le même arbre de fichiers (`attach_to_tree`) : une seule copie
physique, chaque serveur l'indexe à sa façon, et l'arbre reste une copie (une seule écriture).

Ce qui est refusé à la liaison, et à chaque passe :

- une étagère sous 18+ vers une destination qui ne cloisonne pas les comptes
  (`shelf.destination_not_partitioned`) : CWA et Komga non audité ne cloisonnent rien ;
- un sous-dossier qui remonte (`..`), absolu, ou qui sort de la racine par un lien
  (`target.folder_outside_root`) ;
- un transport que la destination ne propose pas (`target.transport_not_supported`).

## Le compte Kavita et la clé de chaque membre

La progression de chaque membre se synchronise avec **sa** clé, jamais avec le compte de
service : voir [sync.md](sync.md). Chaque adulte lie sa clé depuis **Réglages, Compte Kavita**
(ou `GET|PUT|DELETE /api/v1/profiles/{id}/destination-accounts[/liaison]` : lire, lier ou
renouveler, révoquer) : la clé est vérifiée contre
Kavita, scellée, et les réponses ne portent que son empreinte. Un administrateur saisit la clé
d'un profil enfant, pour le compte Kavita déclaré dans son profil ; un enfant ne lie rien. Une
clé révoquée cesse d'être utilisée à la passe suivante.

## Retirer une destination, détacher Kavita

L'écran **Réglages, Destinations** (et `GET /api/v1/destinations`) liste les liaisons avec leur
modèle de nommage. Deux opérations y sont proposées, chacune derrière une action réservée et une
phrase tapée :

- **Retirer une destination** (`POST /api/v1/destinations/{id}/removal-preview`, puis
  `/removal`) : garder les fichiers (`--keep-files`, réponse immédiate) ou les supprimer
  (`--delete-files`, une tâche de fond, réponse `202` avec son identifiant). En ligne de
  commande : `shelfhold target remove <liaison> --keep-files|--delete-files --confirm <phrase>`.
  Une suppression de fichiers interrompue s'abandonne avec `--give-up` : la base est réglée,
  sans attendre ce que les fichiers n'ont pas rendu.
- **Détacher Kavita** (`POST /api/v1/targets/{id}/detach-preview`, puis `/detach`) : tout
  défaire — liaisons, arbre, secrets — en une tâche de fond. Le mot de passe du compte Kavita est
  demandé une dernière fois et ne passe qu'en mémoire.

`shelfhold target recheck <cible>` relance un contrôle de santé, `target reconnect` une
reconnexion. Le pas-à-pas complet (y compris le montage du dossier média dans le compose) est
dans [installation-headless.md](installation-headless.md) et [adopter-kavita.md](adopter-kavita.md).

## Ce qui n'existe pas encore

- **Un écran de création** d'une cible (Komga, CWA, dossier natif) et une étape de l'assistant pour
  Komga ou CWA : pas encore disponible (la ligne de commande et l'API suffisent).
- **La synchronisation de la progression** s'allume par liaison (`shelfhold target sync-progress`, case de
  l'écran des destinations) et seul Kavita la tient — voir [sync.md](sync.md).
- **Les collections et listes poussées vers Kavita** : livrées, voir
  [collections-kavita.md](collections-kavita.md) ; les œuvres liées dans
  [oeuvres-liees.md](oeuvres-liees.md).
