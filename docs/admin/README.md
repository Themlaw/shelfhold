# Documentation d'administration

Pages pour qui installe et fait vivre une instance Shelfhold. Les clés de configuration sont dans
[`docs/configuration.md`](../configuration.md) (explications) et dans
[`docs/reference/config.md`](../reference/config.md) (référence générée du code), les décisions d'exécution dans
[`docs/decisions/M1a.md`](../decisions/M1a.md) et [`docs/decisions/M1b.md`](../decisions/M1b.md).

`shelfhold <commande> --help` fait foi sur ce que votre version propose. Ce qui est annoncé pour plus tard
est dit comme tel dans la page : le second registre d'images (Codeberg) est à venir et ne change rien à
l'installation.

| Je veux… | Page |
| --- | --- |
| Installer sans interface, jusqu'au premier chapitre | [installation-headless.md](installation-headless.md), clés et fichier déclaratif dans [headless.md](headless.md) |
| Brancher une bibliothèque Kavita qui existe déjà | [adopter-kavita.md](adopter-kavita.md) |
| Remplacer mes scripts (Suwayomi vers Kavita) | [migration-depuis-scripts.md](migration-depuis-scripts.md) |
| Laisser Sablier arrêter Suwayomi ou Kavita, et les réveiller à la demande (mode piloté) | [reveil-sablier.md](reveil-sablier.md) |
| Suivre des séries depuis Shelfhold, Suwayomi multi-comptes, réglages à désactiver dans Suwayomi | [suwayomi-multi-comptes.md](suwayomi-multi-comptes.md) |
| Sortir un volume de Suwayomi avant de le démonter | [rapatrier-avant-demontage.md](rapatrier-avant-demontage.md) |
| Défaire la bascule, ou revenir à la 0.2.0 | [retour-arriere.md](retour-arriere.md) |
| Savoir ce que Shelfhold attend de moi, et quelle commande répond | [etats-attente-et-sorties.md](etats-attente-et-sorties.md) |
| Sauvegarder et restaurer | [sauvegarde.md](sauvegarde.md) (sauvegarde intégrée), [restauration.md](restauration.md) (ce que l'hôte sauvegarde, restauration) |
| Une racine, `/store` ou la base manquent et il n'y a pas de sauvegarde | [sinistre-sans-sauvegarde.md](sinistre-sans-sauvegarde.md) |
| Comprendre « libre de droits aux États-Unis », régler le pays de l'instance | [domaine-public.md](domaine-public.md) |
| Remplacer un chapitre par une meilleure version, ce que voient Kavita et KOReader | [mise-a-niveau.md](mise-a-niveau.md) |
| Révisions republiées par une source, version suspecte, chapitre retiré à la source | [revisions-en-attente.md](revisions-en-attente.md) |
| Effacer pour de bon (`purge --now`, `--checkpoint`, `--leftovers`) | [purge.md](purge.md) |
| Garder une copie dans `/store`, « Mettre à l'abri », purge chez Suwayomi, l'index de `/store` | [copie-conservee.md](copie-conservee.md) |
| Importer vos propres fichiers, fichiers protégés et audio refusés, tatouage | [dossier-import.md](dossier-import.md) |
| Comprendre la pause « disque presque plein » | [garde-disque.md](garde-disque.md) |
| Mettre à jour ou recréer le conteneur | [redeploiement.md](redeploiement.md) |
| Plusieurs disques sous `/library` | [topologie-racines-physiques.md](topologie-racines-physiques.md), [montage-adopt-ro.md](montage-adopt-ro.md) |
| Se connecter avec une clé d'accès, la révoquer, que faire si l'appareil est perdu | [passkeys.md](passkeys.md) |
| Durcir, exposer sur Internet | [durcir.md](durcir.md), [exposition-publique.md](exposition-publique.md) |
| Plugins tiers (désactivés par défaut, installation, activation, attestation) | [plugins-tiers.md](plugins-tiers.md) |
| Réglages d'une source (secrets et cookies chiffrés, réglages réservés) | [sources.md](sources.md) |
| Laisser des enfants lire : étagère famille, un compte Kavita par lecteur, appareil partagé | [famille.md](famille.md) |
| Règles du contrôle parental, âge dans Kavita, retrait, livres EPUB (âge de série poussé verrouillé, omnibus) | [controle-parental.md](controle-parental.md) |
| Classer les œuvres (« À classer », « À ranger », `shelfhold rate`) | [a-classer.md](a-classer.md) |
| Qui peut faire quoi : rôles, actions réservées, fraîcheur du mot de passe | [roles.md](roles.md) |
| Conditions, limites et `terms_url` de chaque source native | [sources.md](sources.md) |
| Suivre une série sur plusieurs sources, rang, repli, cadence, trous de numérotation | [suivi-multi-sources.md](suivi-multi-sources.md) |
| Métadonnées OpenLibrary, verrous de champ, rattachement d'Éditions | [metadonnees.md](metadonnees.md) |
| Ce que fait le neutraliseur d'un EPUB non fiable, PDF et SVG | [neutraliseur.md](neutraliseur.md) |
| Un achat tatoué : ce que Shelfhold en fait | [fichiers-tatoues.md](fichiers-tatoues.md) |
| Vue d'ensemble des révisions (mise à niveau, quarantaine, file) | [revisions.md](revisions.md) |
| Brancher votre propre instance lncrawl (romans web en EPUB, sans crawler fourni) | [lncrawl.md](lncrawl.md) |
| Secrets, notifications, qui possède quel champ | [secrets.md](secrets.md), [notifications.md](notifications.md), [proprietaire-des-champs.md](proprietaire-des-champs.md) |
| Lire les journaux, régler leur niveau | [journaux.md](journaux.md) |
| Joindre un paquet de diagnostic à une demande d'aide | [diagnostic.md](diagnostic.md) |
| Dimensionner la machine | [budget-ram.md](budget-ram.md) |
| Plateformes (Docker, Unraid, TrueNAS, Synology, Podman), prérequis, veille du NAS | [plateformes.md](plateformes.md), [nas-energie.md](nas-energie.md) |
| Avec quoi lire : navigateurs, liseuses, KOReader, OPDS, colonne « hors ligne » | [clients.md](clients.md) |
| Désinstaller sans rien perdre (sauvegarde, kit de secours, `/store`) | [desinstaller.md](desinstaller.md) |
| Écrire un plugin (source, destination, kit, gabarit) | [../plugins/parcours.md](../plugins/parcours.md) |
| Page à donner à la famille | [../famille/README.md](../famille/README.md) |
