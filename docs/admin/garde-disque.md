# Garde disque, stockage réseau et arrêt propre

## Garde disque (ENF-OPS-006)

Shelfhold mesure l'espace libre de `/data`, de `/store` et de chaque racine physique de
`/library`, chaque minute et au démarrage.

| | Seuil par défaut | Réglage |
| --- | --- | --- |
| Pause | moins de `max(5 %, 10 Gio)` libres | `SHELFHOLD_DISK_PAUSE_PERCENT`, `SHELFHOLD_DISK_PAUSE_GIB` |
| Reprise | plus de `max(8 %, 15 Gio)` libres | `SHELFHOLD_DISK_RESUME_PERCENT`, `SHELFHOLD_DISK_RESUME_GIB` |

Les seuils de reprise doivent être strictement au-dessus de ceux de pause, pour que l'état ne
bascule pas sans cesse. La mesure est celle de l'espace utilisable par un processus non privilégié.

**Pendant la pause** : les tâches d'écriture sont en pause (code `disk.low`), **sans tentative
dépensée ni réessai** ; la lecture, l'interface et les sondes continuent (`/readyz` répond). Les écritures qui ajoutent des octets sont refusées par la couche fichiers elle-même ;
renommer, déplacer dans le même volume et supprimer restent possibles. `shelfhold status` et
`/healthz` (authentifié) listent les files en pause et la raison. L'événement `disk.low` est émis
une fois à chaque passage sous le seuil, `ops.disk_recovered` à la reprise.

**La garde ne supprime jamais rien.** Libérez de la place vous-même (copies inutiles, Suwayomi,
corbeille) ; les files reprennent seules au-dessus du seuil de reprise. La purge anticipée de la
corbeille sous pression disque arrive avec M3.

## Stockage réseau

`/data` sur NFS, CIFS/SMB, 9p, Ceph, AFS ou sshfs est **refusé au démarrage** : SQLite en mode WAL
et les verrous de fichier y sont peu fiables. Mettez `/data` sur un disque local (SSD de
préférence). Pour passer outre à vos risques : `SHELFHOLD_ALLOW_NETWORK_DATA_DIR=true` (un
avertissement est journalisé à chaque démarrage). `/store` et `/library` peuvent être sur le
réseau ou sur un disque dur ; leur sentinelle les protège d'un montage manquant
([topologie-racines-physiques.md](topologie-racines-physiques.md)).

## Arrêt propre (ENF-OPS-007)

À l'arrêt du conteneur (SIGTERM) : plus aucune tâche n'est prise ; Shelfhold attend jusqu'à 25 s
que les tâches coopératives s'arrêtent entre deux étapes (elles reprennent là au démarrage) ; puis
il libère ses verrous, en dernier celui de l'instance. Une tâche qui ne finit pas est abandonnée
et son **intention de fichier reste ouverte** : au démarrage suivant, elle est rejouée ou annulée
avant toute autre tâche, sans doublon ni perte. Le compose laisse 60 s (`stop_grace_period`) : ne
le réduisez pas sous 30 s (le délai d'attente des tâches est de 25 s).

Un arrêt brutal (kill -9, coupure) est traité de la même façon au redémarrage. Voir
[redeploiement.md](redeploiement.md).
