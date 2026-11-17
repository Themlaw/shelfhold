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
| **Komga** | une bibliothèque Komga ; le cloisonnement par bibliothèque est prouvé par l'audit des comptes | **pas encore disponible** (voir plus bas) |
| **CWA** | la boîte de dépôt de Calibre-Web Automated : un EPUB déposé, l'importeur le prend | **pas encore disponible** (voir plus bas) |

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

### Komga et CWA : la destination existe, sa création pas encore

Le code qui parle à Komga (scan de bibliothèque débouncé, regroupement par dossier, cloisonnement
par bibliothèque prouvé par l'audit des comptes, plancher 1.22.0) et celui qui dépose dans la
boîte CWA (EPUB seul, dépôt atomique, suivi par identifiant) sont livrés et testés. **Aucune
commande, route ni écran ne crée encore une cible Komga ou CWA** : `target add komga` et
`target add cwa` n'existent pas, et la liaison d'une étagère n'accepte que des cibles déjà
déclarées. Ce sera une action réservée à part entière quand elle arrivera. Jusque-là, ces
destinations ne sont pas utilisables sur une instance de production.

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

- **Créer une cible Komga ou CWA** : pas encore disponible (voir plus haut).
- **Activer la synchronisation de la progression pour une liaison** : le réglage existe côté
  données, aucune interface ne l'écrit encore — voir [sync.md](sync.md).
- **Les collections et listes poussées vers Kavita** : livrées, voir
  [collections-kavita.md](collections-kavita.md) ; les œuvres liées dans
  [oeuvres-liees.md](oeuvres-liees.md).
