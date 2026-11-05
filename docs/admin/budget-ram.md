# Budget mémoire et disque

Shelfhold est un seul conteneur, mais il vit à côté de Suwayomi et de Kavita, qui pèsent plus
lourd que lui. Cette page donne le budget du **profil de référence** (ENF-OPS-005) : Shelfhold,
Suwayomi et Kavita sur une même machine, dans l'état de la version 0.2, avant le traitement
d'images de M1b.

## Profil de référence : mémoire

| Service | Au repos | En pic | Source |
| --- | --- | --- | --- |
| Shelfhold | **68,3 Mo** | **72,0 Mo** après 8 000 requêtes (pic du cgroup 79,0 Mo) | mesuré, spike S4 |
| Suwayomi | au moins 750 Mo | selon les extensions et les mises à jour | estimation du jalon, à mesurer en M1b |
| Kavita | environ 200 Mo | jusqu'à 2 Go pendant un gros scan | estimation du jalon |
| **Total à prévoir** | environ 1 Go au repos | **3 Go** avec un scan de Kavita | |

Prévoyez **4 Go** de mémoire pour la machine, dont au moins 3 Go libres pour ces trois services.

### Ce qui est mesuré, et ce qui ne l'est pas

Les chiffres de Shelfhold viennent du spike S4 (`spikes/s4/RESULTATS.md`), sur l'image du dépôt,
amd64, avec la pile de test durcie et `MALLOC_ARENA_MAX=2` :

- processus `shelfhold serve`, 30 s après le démarrage : **68,3 Mo** (Pss 64,3 Mo) ;
- juste après 8 000 requêtes sur 16 fils : **72,0 Mo** ; 15 s après la charge : 71,7 Mo (stable,
  pas de fuite visible) ;
- image compressée : **57,5 Mo**, pour un plafond de 300 Mo ; sur disque : 244 Mo.

Ces mesures sont celles du squelette : elles ne comptent ni Pillow ni libvips. Elles seront
refaites en M1b avec le traitement d'images, et la cible reste de 250 Mo au repos et 500 Mo en pic
pour Shelfhold seul. Les chiffres de Suwayomi et de Kavita sont ceux du cahier des charges, pas
des mesures de ce dépôt.

### Les limites du compose

Le compose d'exemple pose `mem_limit: 768m` sur le service Shelfhold et `mem_limit: 512m` sur
`shelfhold-migrate`, avec `pids_limit` et un `/tmp` en mémoire de 256 Mo. C'est de la marge
(dix fois la consommation au repos) : si le conteneur est tué par le noyau (OOM), regardez
d'abord la taille des CBZ reçus (leur plafond est configurable) avant d'augmenter la limite. Ne
retirez pas `mem_limit` : la limite protège Suwayomi et Kavita voisins.

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
