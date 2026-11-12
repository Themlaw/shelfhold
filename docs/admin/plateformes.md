# Plateformes et matériel

Shelfhold est un conteneur, livré en images **amd64 et arm64** natives. Il vit à côté de Suwayomi et de Kavita.

## Ce qui est testé, ce qui est documenté

- **Testé** : Docker sur Debian, amd64 et arm64 (la CI construit les deux images ; le parcours E2E sur l'image est
  à venir avec le lot L57).
- **Documenté, non testé par le projet** : Unraid, TrueNAS, Synology, Podman. Les étapes ci-dessous sont celles
  de l'image et du compose d'exemple (`compose.yaml`) ; l'interface de chaque plateforme évolue, vérifiez-la.
  Un retour d'expérience est bienvenu en issue.

## Prérequis matériels

| | Minimum | Recommandé |
| --- | --- | --- |
| Mémoire de la machine | 4 Go, dont 3 Go libres pour Shelfhold, Suwayomi et Kavita | 8 Go si Kavita scanne de grosses bibliothèques |
| Processeur | 2 cœurs, amd64 ou arm64 | 4 cœurs |
| Disque de `/data` | disque local, quelques Go | SSD ; **jamais NFS** (base SQLite) |
| Disque de `/store` | séparé de `/data` ; prévoir de x2 à x3 la taille des livres gardés | disque redondant |
| Bibliothèque | selon votre collection | disque distinct de `/data` |

Le détail et les mesures sont dans [budget-ram.md](budget-ram.md).

Tous les dossiers montés doivent être inscriptibles par l'**identifiant 1000** (utilisateur du conteneur) :

```sh
mkdir -p data store && chown 1000:1000 data store && chmod 750 data store
```

## Docker sur Debian (amd64, arm64)

Installez Docker Engine et le plugin compose depuis les paquets officiels de Docker, copiez `compose.yaml`, créez
les dossiers ci-dessus, puis `docker compose up -d`. Le profil durci du compose (système de fichiers en lecture
seule, `cap_drop: [ALL]`) est celui qui est testé.

## Unraid

Créez les dossiers de `/data` et `/store` sur un dossier **disque ou cache** (pas un dossier réseau distant),
donnez-les à l'identifiant 1000, puis déployez le compose avec le gestionnaire de compose ou une application
Docker personnalisée. Gardez `/data` sur le cache (SSD) pour la base.

## TrueNAS

Créez deux jeux de données (`data`, `store`) avec le propriétaire d'identifiant 1000, puis déployez le compose
comme application personnalisée. Une application TrueNAS tourne avec ses propres réglages d'utilisateur :
laissez `user: "1000:1000"` et donnez les droits d'écriture à cet identifiant sur les jeux de données.

## Synology

Avec Container Manager, créez un projet depuis le compose. Créez les dossiers, puis donnez à
l'identifiant 1000 les droits de lecture et d'écriture dans les permissions du dossier (les listes de contrôle
d'accès de DSM peuvent masquer un refus : vérifiez en écrivant un fichier de test).

## Podman

Rootless, le compose s'exécute avec `podman compose` ou `podman-compose`. L'identifiant 1000 du conteneur
correspond à un identifiant décalé sur l'hôte : montez avec `:U` ou utilisez `--userns=keep-id`. Sous SELinux,
ajoutez `:Z` aux volumes. Le profil durci (`read_only`, `cap_drop`) est le même.

## Énergie et veille

Un NAS qui s'éteint ou met ses disques en veille : [nas-energie.md](nas-energie.md).
