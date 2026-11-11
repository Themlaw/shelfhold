# Suivre des séries avec Suwayomi : comptes, réglages et suivi

Cette page couvre le service connecté Suwayomi : le mode d'authentification, le compte de service,
les deux réglages de Suwayomi à désactiver, suivre et ne plus suivre une série, et ce qui se passe quand
Suwayomi est réinstallé. L'installation de bout en bout est dans
[installation-headless.md](installation-headless.md).

## Réglages de Suwayomi à désactiver

Suwayomi applique à sa mise à jour automatique deux filtres globaux : `excludeUnreadChapters` (ignorer les
séries qui ont des chapitres non lus) et `excludeNotStarted` (ignorer les séries pas commencées). Sur le
serveur épinglé, **les deux sont activés par défaut**. Les séries que Shelfhold suit ne sont lues par
personne dans Suwayomi : avec ces filtres, elles risquent de ne plus être rafraîchies, et Shelfhold ne verrait
plus de nouveau chapitre.

**Désactivez les deux** dans Suwayomi avant d'ajouter la source. `shelfhold source add suwayomi` est
refusé tant qu'ils sont actifs (`source.update_filters_blocking`) et dit lequel. Shelfhold ne les règle
pas à votre place : c'est une écriture dans le réglage de Suwayomi. Si Suwayomi ne répond pas
lisiblement à la question, l'ajout n'est pas bloqué.

## Ajouter Suwayomi

    shelfhold source add suwayomi --url http://suwayomi:4567 --secret-file <fichier>

`--secret-file` contient `utilisateur:mot_de_passe` sur une ligne (`-` : entrée standard). Shelfhold lit
le mode d'authentification du serveur :

| Mode de Suwayomi | Ce que Shelfhold fait |
| --- | --- |
| aucun | sans secret |
| `basic_auth` | identifiants Basic, un seul compte |
| `ui_login` (multi-comptes) | connexion par jeton avec un **compte de service** |
| autre (SAML, etc.) | refusé : `source.auth_mode_unsupported` |

### Mode `ui_login` : le compte de service

Dans Suwayomi multi-comptes, chaque compte a sa propre bibliothèque. Shelfhold ne suit que **celle de son
compte de service**.

1. Créez dans Suwayomi un compte dédié à Shelfhold, **aux droits minimaux** : qu'il ne puisse ni
   installer des extensions ni administrer le serveur. Un compte trop puissant est refusé
   (`source.auth_mode_unsupported`). Le mot de passe fait **8 caractères au moins**.
2. Ajoutez la source avec ce compte. Le compte est vérifié (connexion, puis lecture de ses rôles)
   avant tout enregistrement.
3. Si le serveur n'expose pas les rôles (c'est le cas du serveur épinglé), Shelfhold ne peut pas vérifier
   les droits : la source est ajoutée, avec l'avertissement durable `roles_unverified` dans
   `shelfhold status`. Rien n'est en pause, mais Shelfhold **n'efface jamais** rien chez Suwayomi tant
   que l'avertissement est là. Quand vous avez vérifié vous-même que le compte est minimal,
   `shelfhold source confirm-roles` l'acquitte (action réservée, auditée).
4. Mettez les séries à suivre dans la **bibliothèque de ce compte**. Les bibliothèques des autres comptes
   ne sont jamais lues, et aucune autre session n'est ouverte.

Les jetons vivent en mémoire du processus, jamais en base ni sur disque. Le mot de passe du compte est
scellé comme tout secret ([secrets.md](secrets.md)). Un mot de passe refusé (`auth_expired`) se remplace par
`shelfhold source set-secret --secret-file <fichier>`.

Pour une purge, Shelfhold ne retire que la revendication du compte de service. Un fichier qu'un autre compte
garde dans Suwayomi n'est jamais supprimé, et la place libérée est mesurée plutôt que supposée
([purge.md](purge.md)).

## Suivre et ne plus suivre depuis Shelfhold

Suwayomi sait lister sa bibliothèque ; la série se désigne par la clé de sa source et son identifiant
chez cette source :

    shelfhold follow suwayomi:7001 <identifiant-de-la-série>
    shelfhold unfollow suwayomi:7001 <identifiant-de-la-série>
    shelfhold unfollow suwayomi:7001 <identifiant-de-la-série> --remove-upstream --confirm

- **Suivre** : une série déjà dans la bibliothèque de Suwayomi y est rattachée telle quelle ; une série que
  Suwayomi connaît mais qui n'est pas dans sa bibliothèque y entre et reçoit la catégorie « Shelfhold ».
  Une série que Suwayomi ne connaît pas est refusée : Shelfhold n'en crée pas.
- **La catégorie « Shelfhold »** est exclue du téléchargement automatique de Suwayomi (Shelfhold demande
  lui-même les chapitres, un à un). Elle n'est **pas** exclue des mises à jour : les séries restent
  rafraîchies. `shelfhold detach --restore-upstream` la retire ([retour-arriere.md](retour-arriere.md)).
- **Ne plus suivre** retire la série de la catégorie et la garde dans Suwayomi. `--remove-upstream` la sort
  aussi de la bibliothèque de Suwayomi et exige `--confirm` (sinon code de sortie 3 et la commande exacte).
  Aucun fichier n'est supprimé : ce n'est pas une action réservée.
- Une demande approuvée ne crée qu'**une** entrée dans Suwayomi, même en cas de double envoi ou de deux
  processus.
- Refusé tant que le service est en pause (`follow.service_paused`) : levez d'abord la pause
  (`shelfhold status`). Les routes sont `POST` et `DELETE /api/v1/follows`, réservées à l'admin.
- `shelfhold follow gap-ack <édition> <numéro>` acquitte un numéro volontairement absent (`--undo` le
  reprend).

## Ce que Shelfhold surveille

- **Une source non chargée.** À chaque cycle, Shelfhold lit la liste des sources de Suwayomi. Une source
  absente, remplacée par un bouchon ou à moitié mise à jour est déclarée non chargée : ses séries ne sont
  ni demandées ni comptées en échec, et `source.not_loaded` vous prévient une fois par épisode.
  Réinstallez l'extension dans Suwayomi.
- **Des chapitres jamais téléchargés.** Un chapitre demandé par Shelfhold et toujours absent après 6 heures,
  quand la file de Suwayomi est vide, est redemandé, 20 chapitres au plus par cycle, jamais un chapitre
  sous pierre tombale. Si Suwayomi ne sait pas dire l'état de sa file, rien n'est relancé.
- **Des séries silencieuses.** Un seul événement `series.stale` par cycle (un nombre) ; `shelfhold status`
  les liste une à une.
- **Un 401 répété** met le suivi en pause (`auth_expired`) sans compter d'erreur de contenu ni créer de
  candidate à la mise à l'abri.
- **Une erreur de contenu** n'est comptée que pour ce qui l'est : seules les erreurs de contenu d'une même
  source amont, au moins deux de suite, rendent un chapitre candidat à la mise à l'abri
  ([copie-conservee.md](copie-conservee.md)). Une panne, un 5xx, un réveil raté ne comptent jamais.

## Suwayomi réinstallé

Shelfhold reconnaît son installation par l'identité de la catégorie « Shelfhold ». Si elle a changé, le suivi
passe en pause (`source_instance_changed`) **sans rien supprimer ni redemander**. Remettez vos séries dans
le nouveau Suwayomi, puis :

    shelfhold source reconnect

La nouvelle installation devient la référence ; les demandes ouvertes qui portaient un ticket de
l'ancienne sont redemandées. Une pause `auth_expired` n'est pas levée par `reconnect` :
`shelfhold source recheck` revérifie la source et reprend le suivi.

## Suwayomi derrière Sablier

Voir [reveil-sablier.md](reveil-sablier.md) : réveil avant chaque lot et mode piloté
(`source.check_mode`).
