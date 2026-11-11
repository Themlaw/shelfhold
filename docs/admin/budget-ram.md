# Budget mémoire et disque

Shelfhold est un seul conteneur, mais il vit à côté de Suwayomi et de Kavita, qui pèsent plus
lourd que lui. Cette page donne le budget du **profil de référence** (ENF-OPS-005) : Shelfhold,
Suwayomi et Kavita sur une même machine. Le chiffre de l'image à vide date de la version 0.2 ; les bancs
de M1b (plus bas) mesurent le profil de référence avec le traitement d'images.

## Profil de référence : mémoire

| Service | Au repos | En pic | Source |
| --- | --- | --- | --- |
| Shelfhold | **102,8 Mo** | **133,9 Mo** après 8 000 requêtes (pic du cgroup 137,1 Mo) | mesuré sur l'image de la 0.2, voir ci-dessous |
| Suwayomi | au moins 750 Mo | selon les extensions et les mises à jour | estimation du jalon, non mesurée dans ce dépôt |
| Kavita | environ 200 Mo | jusqu'à 2 Go pendant un gros scan | estimation du jalon |
| **Total à prévoir** | environ 1,1 Go au repos | **3 Go** avec un scan de Kavita | somme des lignes ci-dessus |

Prévoyez **4 Go** de mémoire pour la machine, dont au moins 3 Go libres pour ces trois services.

### Ce qui est mesuré, et ce qui ne l'est pas

**Shelfhold, version 0.2** (mesuré le 2026-11-06). Image construite avec `docker/Dockerfile` à partir
du code de M1a (migrations 0002 à 0010, planificateur, notifications), amd64, lancée comme le compose
d'exemple (`read_only`, `cap_drop: [ALL]`, `/tmp` en mémoire, `MALLOC_ARENA_MAX=2` de l'image), base
migrée et vide, aucun service connecté :

- processus `shelfhold serve`, 45 s après `/readyz` : **102,8 Mo** de mémoire résidente ;
- juste après 8 000 requêtes sur 16 fils (`/healthz`, `/readyz`, `/api/v1/about`, 404) : **133,9 Mo**,
  inchangé 15 s plus tard (pas de baisse, pas de croissance non plus) ; pic du cgroup 137,1 Mo ;
- image compressée : **78,8 Mo** (gzip de `docker save`), pour un plafond de 300 Mo ; sur disque : 338 Mo.

C'est environ 35 Mo de plus que le squelette de M0 : les dépendances de M1a (planificateur, Apprise,
SQLAlchemy et le schéma, chargement des modules) s'y ajoutent. Une instance qui a des centaines de
séries, des tâches en cours et un scan à l'arrière-plan pèsera davantage : ce chiffre est un plancher
mesuré à vide, pas une garantie.

**Squelette de M0** (spike S4, `spikes/s4/RESULTATS.md`, 2026-11-04, même méthode) : la référence de
départ, avant M1a.

- processus `shelfhold serve`, 30 s après le démarrage : **68,3 Mo** (Pss 64,3 Mo) ;
- juste après 8 000 requêtes sur 16 fils : **72,0 Mo** ; 15 s après la charge : 71,7 Mo (stable,
  pas de fuite visible) ;
- image compressée : **57,5 Mo**, pour un plafond de 300 Mo ; sur disque : 244 Mo.

Ces mesures de la 0.2 ne comptent ni Pillow ni libvips : les bancs de M1b ci-dessous, qui les
comptent, les complètent. La cible reste de 250 Mo au repos et 500 Mo en pic pour Shelfhold seul, et la 0.2 les
respecte. Les chiffres de Suwayomi et de Kavita sont ceux du cahier des charges, pas des mesures de
ce dépôt.

Pour refaire la mesure : construire l'image, lancer `shelfhold migrate` puis `shelfhold serve` avec les
options du compose, lire `VmRSS` dans `/proc/<pid>/status` du processus Python du conteneur, puis
rejouer la charge et relire `VmRSS` et `memory.peak` du cgroup.

### Les bancs de mémoire (M1b)

Deux bancs mesurent la mémoire résidente (`VmRSS` lu dans `/proc`, processus serveur **et tous ses
enfants**, dont les workers de décodage d'images) sur le profil de référence généré
(`backend/tests/bench/generator.py` : 2 000 Œuvres, 60 000 Unités, 500 séries suivies, 6 comptes ;
`MALLOC_ARENA_MAX=2` comme dans l'image) :

| Banc | Seuil | Mesuré (M1b, 2026-11-08, hors conteneur) | Où il tourne |
| --- | --- | --- | --- |
| Repos, 3 s après `/readyz` | **< 250 Mo** | 135 Mo | chaque PR (`backend-bench`) |
| Pic pendant l'export de 500 chapitres, avec 50 décodages d'images | **< 500 Mo** | 180 Mo (processus + un worker) | nuit et manuel (`nightly.yml`, job `bench-rss`) |
| Pic, variante légère de 50 chapitres | **< 500 Mo** | 163 Mo | chaque PR |

Le banc lourd reste hors des PR : il construit les 60 000 Unités, ingère et rend 500 archives puis
échantillonne `/proc` pendant tout l'export, ce qui n'a de sens que sur un runner au calme. Il est
justifié dans `tools/traceability-exceptions.toml`. Les seuils viennent de
`.github/bench-reference.json`, versionné dans le dépôt (les artefacts de workflow expirent) et joint
à chaque release par `release.yml`. Ces mesures sont celles d'un processus Python sur le profil CI
(fichiers creux) : le profil complet de 2 To se mesure avant une release, hors CI.

**Limite d'espace d'adressage du worker de décodage.** Avant de lire le moindre octet d'image, le
worker pose `RLIMIT_AS` (limite souple et dure, irrévocable) à `WorkerLimits.memory_bytes`, calculé
d'après le plafond de pixels du type d'image. Une image dont la décompression dépasserait ce plafond
est refusée avec `image.decode_failed` (ou `image.too_large` quand l'en-tête suffit) ; seul le
worker échoue, le serveur continue. Le test `test_decode_worker_rlimit_as_set` le lit dans
`/proc/<pid>/limits` d'un worker vivant.

**Fenêtre de maintenance.** La vérification du rangement (30 min), la comparaison de hash (1 h) et
l'intégrité par roulement (2 h) totalisent 3 h 30 sur les 4 h de la fenêtre par défaut. Un banc les
rejoue sur une horloge injectée à travers l'ordonnanceur et les curseurs : sur un disque plus lent,
le job le moins prioritaire s'arrête avec la fenêtre et reprend à son curseur la nuit suivante, sans
refaire un pas déjà fait.

### Les limites du compose

Le compose d'exemple pose `mem_limit: 768m` sur le service Shelfhold et `mem_limit: 512m` sur
`shelfhold-migrate`, avec `pids_limit` et un `/tmp` en mémoire de 256 Mo pour le service. C'est de la
marge (plus de cinq fois le pic mesuré) : si le conteneur est tué par le noyau (OOM), regardez
d'abord la taille des CBZ reçus (leur plafond, 512 Mo par défaut, se règle avec
`SHELFHOLD_RECEIVED_CBZ_MAX_BYTES`, [configuration.md](../configuration.md)) avant d'augmenter la
limite. Ne retirez pas `mem_limit` : la limite protège Suwayomi et Kavita voisins.

## Profil de référence : disque

Le budget disque dépend du mode de rangement. « ×1 » est la taille de votre bibliothèque.

| Mode | Copies | Budget |
| --- | --- | --- |
| **Mode passage** (défaut) | le fichier traverse le staging puis est rangé dans la bibliothèque ; Suwayomi garde aussi le sien | environ **×2** (Suwayomi + bibliothèque) ; **×1** si vous purgez le téléchargement dans Suwayomi une fois rangé |
| **Copie conservée** (option par étagère, M1b) | une copie indépendante dans `/store`, en plus | **×2 à ×3** ; reflink si le système de fichiers le permet (btrfs, XFS) pour ne pas payer deux fois ; **×1 à ×2** avec le lien dur optionnel, réservé au contenu remplaçable (jamais pour une Unité irremplaçable) |

Jamais trois exemplaires en mode passage. En plus : la base et le journal (quelques dizaines de
Mo pour des milliers de séries), les sauvegardes de migration dans `/data/backups/pre-migration`
(une base de plus par migration) et la corbeille de chaque racine. Sous le seuil de
[garde-disque.md](garde-disque.md) (5 % ou 10 Gio libres), les écritures se mettent en pause :
dimensionnez avec cette réserve.

## Choix du matériel

- `/data` sur SSD local. Les disques de `/library` et `/store` peuvent être des disques durs :
  les tâches périodiques lourdes tiennent dans la fenêtre de maintenance (03:00 à 07:00) pour
  les laisser se mettre en veille.
- Un seul conteneur Shelfhold par jeu de volumes ([redeploiement.md](redeploiement.md)).
