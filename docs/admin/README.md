# Documentation d'administration

Pages pour qui installe et fait vivre une instance Shelfhold. Les clés de configuration sont dans
[`docs/configuration.md`](../configuration.md) (explications) et dans
[`docs/reference/config.md`](../reference/config.md) (référence générée du code), les décisions d'exécution dans
[`docs/decisions/M1a.md`](../decisions/M1a.md).

Une commande marquée **[M1b]** n'existe pas encore en 0.2 : la page dit ce qu'on fait d'ici là.
`shelfhold <commande> --help` fait foi sur ce que votre version propose.

| Je veux… | Page |
| --- | --- |
| Installer sans interface, jusqu'au premier chapitre | [installation-headless.md](installation-headless.md), clés et fichier déclaratif dans [headless.md](headless.md) |
| Brancher une bibliothèque Kavita qui existe déjà | [adopter-kavita.md](adopter-kavita.md) |
| Remplacer mes scripts (Suwayomi vers Kavita) | [migration-depuis-scripts.md](migration-depuis-scripts.md) |
| Laisser Sablier arrêter Suwayomi ou Kavita, et les réveiller à la demande | [reveil-sablier.md](reveil-sablier.md) |
| Sortir un volume de Suwayomi avant de le démonter | [rapatrier-avant-demontage.md](rapatrier-avant-demontage.md) |
| Défaire la bascule | [retour-arriere.md](retour-arriere.md) |
| Savoir ce que Shelfhold attend de moi, et quelle commande répond | [etats-attente-et-sorties.md](etats-attente-et-sorties.md) |
| Sauvegarder et restaurer | [sauvegarde.md](sauvegarde.md) (sauvegarde intégrée), [restauration.md](restauration.md) |
| Comprendre « libre de droits aux États-Unis », régler le pays de l'instance | [domaine-public.md](domaine-public.md) |
| Remplacer un chapitre par une meilleure version, ce que voient Kavita et KOReader | [mise-a-niveau.md](mise-a-niveau.md) |
| Effacer pour de bon | [purge.md](purge.md) |
| Garder une copie dans `/store`, ce que compte la page Santé, l'index de `/store` | [copie-conservee.md](copie-conservee.md) |
| Comprendre la pause « disque presque plein » | [garde-disque.md](garde-disque.md) |
| Mettre à jour ou recréer le conteneur | [redeploiement.md](redeploiement.md) |
| Plusieurs disques sous `/library` | [topologie-racines-physiques.md](topologie-racines-physiques.md), [montage-adopt-ro.md](montage-adopt-ro.md) |
| Se connecter avec une clé d'accès, la révoquer, que faire si l'appareil est perdu | [passkeys.md](passkeys.md) |
| Durcir, exposer sur Internet | [durcir.md](durcir.md), [exposition-publique.md](exposition-publique.md) |
| Plugins tiers (désactivés par défaut, installation, activation, attestation) | [plugins-tiers.md](plugins-tiers.md) |
| Réglages d'une source (secrets et cookies chiffrés, réglages réservés) | [sources.md](sources.md) |
| Étagère famille et livres EPUB : âge de série poussé verrouillé, omnibus, bibliothèque Livres | [controle-parental.md](controle-parental.md) |
| Secrets, notifications, qui possède quel champ | [secrets.md](secrets.md), [notifications.md](notifications.md), [proprietaire-des-champs.md](proprietaire-des-champs.md) |
| Lire les journaux, régler leur niveau | [journaux.md](journaux.md) |
| Joindre un paquet de diagnostic à une demande d'aide | [diagnostic.md](diagnostic.md) |
| Dimensionner la machine | [budget-ram.md](budget-ram.md) |
