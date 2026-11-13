# Rôles, droits et actions réservées

Cette page dit qui peut faire quoi. `shelfhold access-table` affiche la table complète, générée du code
(une ligne par action, rôle et type de crédential) ; `--json` la donne en JSON, `--restricted-profile`
suppose qu'un profil restreint existe.

## Les rôles

| Rôle | Ce qu'il est | Dans cette version |
| --- | --- | --- |
| `admin` | l'administrateur du foyer | le seul compte qui existe : `shelfhold admin create` en crée un, jamais deux |
| `member` | un adulte du foyer | prévu ; il ne reçoit aucune action réservée |
| `child` | un enfant | prévu ; il ne voit que ses propres entrées d'activité |
| `integration` | un outil (webhook, sortie) | ne reçoit ni titre ni entrée privée |

Un rôle inconnu, une action absente du registre ou un sosie d'action sont **refusés**.

Classer une Œuvre est un **droit** (`can_rate`), pas un rôle. Seul l'admin le porte : le drapeau posé à la
main sur un autre compte n'y change rien.

## Qui voit l'activité

Il n'existe pas de lien « parent » : **un adulte compte comme parent**.

- Une entrée `household` est visible, titre compris, de tous les adultes ; un enfant ne voit que
  **ses** entrées.
- Une entrée `private` n'est vue que de son propriétaire. L'admin voit celle d'un enfant en clair et
  celle d'un autre adulte avec le **titre masqué**. Démasquer (`request.unmask`) est une action
  réservée, auditée.
- Une demande sans visibilité est une opération : l'admin seul la voit.

## Crédentials

| Crédential | Actions réservées |
| --- | --- |
| session (mot de passe ou passkey) | oui, avec la fraîcheur ci-dessous |
| CLI locale de l'hôte (socket Unix de `/data`) | oui |
| clé d'API (`read`, `request`, `admin`) | **jamais** : 403 `credential-not-allowed`, portée `admin` comprise |
| jeton d'appareil | **jamais** |

## Les actions réservées

Une action est réservée quand elle supprime pour de bon, révèle ou modifie un secret, touche aux
pierres tombales, au contrôle parental, aux racines ou à la confiance accordée à un plugin. Une
liste fermée d'émetteurs système (nettoyage du staging, corbeille au-delà de 30 jours, fichier sonde)
est exemptée : ils n'agissent que sur des chemins que Shelfhold a lui-même créés.

**Sans profil durci**, une session doit avoir saisi son mot de passe (ou utilisé une passkey) il y a au
plus `SHELFHOLD_FRESH_AUTH_S` secondes (900 par défaut), sinon 401 `reauth-required`, pour :

- effacer : `purge --now`, `purge --leftovers`, vider la corbeille, supprimer une Unité, `detach` complet,
  « tout retirer » d'une destination, effacer `.shelfhold/`, confirmer une suppression après restauration ;
- les secrets : `secrets export`, `rotate`, `import`, changer le secret d'une source, faire tourner la
  clé d'une destination ;
- les pierres tombales : lever, lister, accepter la perte du trousseau, mettre le journal de côté,
  `tombstone rekey` ;
- les racines : `root takeover|reinit`, `store takeover|reinit`, `config set path_map` ;
- `shelf purge-upstream`, `intents resolve`, `normalize migrate`, `source migrate-ids` (aperçu compris : il ouvre les pierres tombales), les révisions (`revisions apply|force`),
  fusionner des Éditions, couvrir un chapitre re-découpé (`coverage resplit`), accepter le risque d'un
  Kavita ouvert (`--accept-admin-risk`).

**Classer** (`rate`) et **enregistrer une passkey** exigent la même fraîcheur, **dans tous les profils**
(décision du porteur du 2026-11-11) : sans mot de passe ou passkey récents, 401 `reauth-required`.

**Sans mot de passe récent** (hors profil durci), l'admin peut : démasquer une demande, créer, faire
tourner ou révoquer une clé d'API ou un jeton, révoquer une passkey, attester un plugin.

**Le contrôle parental** (`shelf create` avec plafond, plafond d'âge, profils autorisés, correspondance
des profils, `target recheck` d'une exposition d'enfant, retrait d'une étagère famille) n'exige un mot de
passe récent que **si un profil restreint existe**.

**Le profil durci** ([durcir.md](durcir.md)) exige la fraîcheur pour toute action réservée, classer et
gérer les clés ou révoquer une passkey compris.

`shelfhold restore acknowledge` ne se fait que depuis la CLI de l'hôte : ni session, ni clé, jamais par
HTTP ([restauration.md](restauration.md)).

## Ouvrir et fermer des accès

    shelfhold admin keys create --name <usage> [--read-only] [--expires-days 90]
    shelfhold admin sessions revoke --all
    shelfhold admin sessions revoke --user <compte>
    shelfhold admin reset-password --username <compte> --password-file <fichier>

Une clé d'API est affichée une seule fois et stockée hachée. `reset-password` change le mot de passe, ferme
toutes les sessions du compte et révoque toutes ses passkeys ([passkeys.md](passkeys.md)).
