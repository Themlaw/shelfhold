# Index de plugins signé : épingler et installer

Un plugin tiers n'arrive jusqu'à une instance que s'il est **référencé par un index signé**,
épinglé à son signataire par l'administrateur. Cette page est le côté administrateur : comment
épingler un dépôt d'index, le rafraîchir, et installer un plugin qu'il vaut. Le côté auteur
est dans [../plugins/index.md](../plugins/index.md) et [../plugins/heberger-un-index.md](../plugins/heberger-un-index.md).

## Épingler un dépôt d'index

Une épingle, c'est une **adresse** et la **clé publique** de son signataire. Sans la clé, il n'y
a pas d'épingle : aucune clé ne livre avec Shelfhold.

```sh
shelfhold plugin index add https://exemple.org/shelfhold-index.json \
  --pubkey "RWT…"
```

`plugin index add` est une action réservée : une session d'administrateur récente, jamais une clé
d'API. Le dépôt est stocké dans la base, avec son url et sa clé.

- `shelfhold plugin index list` : les dépôts épinglés.
- `shelfhold plugin index refresh [url]` : relit un dépôt (ou tous), vérifie la signature et la
  monotonie de la version.
- `shelfhold plugin index remove <url>` : retire le dépôt. Les plugins déjà installés depuis lui
  restent installés, mais ne se mettent plus à jour par cet index.

## Ce qu'un index prouve

- **Signature minisign** préhachée (Ed25519 sur le condensat BLAKE2b-512 du document), avec un
  **commentaire de confiance** signé qui doit redire la `version` et l'`expires` du document.
- **Version monotone** : un index plus ancien que celui déjà vu est refusé.
- **Expiration** à au plus 90 jours : un index expiré est refusé (`plugin.index_expired`).
- **Empreinte sha256** de chaque roue, vérifiée à l'installation.
- **Révocations** : une clé de plugin ou une empreinte de roue révoquée reste révoquée, même
  omise plus tard.
- **Rotation de clé** : pendant une rotation, le document porte `next_pubkey` et est signé par
  la clé épinglée et par la suivante (double signature). L'épingle se déplace sans rien à faire.

Un index qui ne passe pas ces contrôles est refusé avec un code : `plugin.index_invalid`,
`plugin.index_rollback` (version qui recule), `plugin.index_expired`, `plugin.index_not_found`,
`plugin.index_fingerprint_required`.

## Installer depuis un index

```sh
shelfhold plugin install --index https://exemple.org/shelfhold-index.json <clé-du-plugin>
```

La clé du plugin est épinglée au couple (index, signataire) : un autre signataire qui publie la
même clé est une **installation distincte**, jamais une mise à jour. Les anciens états, réglages
et secrets ne bougent que sur la confirmation de l'administrateur (`adopt`, action réservée).

- `min_host` au-dessus de l'hôte : refus à l'installation comme à la mise à jour (`min_host`).
- `api` hors de la fenêtre N / N-1 : le plugin est marqué `incompatible`, événement
  `plugin.incompatible` une seule fois par couple (plugin, raison).
- `deprecated` et `replaced_by` sont lus par l'administrateur ; l'hôte n'installe jamais le
  remplaçant de son propre chef.
- Le mode isolé est recommandé (voir [plugins-isoles.md](plugins-isoles.md)).

## Ce que l'index ne contient pas

Un index ne contient que des références : clé, version, adresse de roue, empreinte. Aucune liste
de sites amont n'est tenue dans le dépôt de Shelfhold : une source de fictions en ligne ne vit
que comme plugin tiers, hors dépôt, sans nom de site ni d'outil ici.

Voir aussi [plugins-tiers.md](plugins-tiers.md), [cycle-de-vie.md](../plugins/cycle-de-vie.md),
[isolation.md](../plugins/isolation.md).