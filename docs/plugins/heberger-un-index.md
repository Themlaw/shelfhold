# Héberger un index tiers

N'importe qui peut héberger un index de plugins : c'est un **fichier JSON signé** servi en HTTPS,
plus sa signature. Le produit ne livre aucune clé et ne fait confiance à aucun dépôt par défaut :
c'est l'administrateur qui épingle, par adresse et par clé publique.

## Ce qu'il faut servir

À la racine d'un site HTTPS ordinaire :

- `index.json` — le document (voir les champs dans [index.md](index.md)) ;
- `index.json.minisig` — la signature détachée (quatre lignes) ;
- `index.json.next.minisig` — seulement pendant une rotation.

Taille maximale crue : 1 Mio. Le client utilise un agent neutre (`shelfhold-plugin-index`) et
suit au plus deux redirections.

## Qui signe, avec quoi

- **Le signataire** est le mainteneur de l'index. Il détient la **clé secrète** minisign ; la clé
  publique (son blob base64) est ce que les administrateurs épinglent. Sa clé d'identifiant
  (16 hexadécimaux) est l'empreinte affichée partout.
- **L'outil** est `minisign`, au format **préhaché** (Ed25519 sur le condensat BLAKE2b-512).
  minisign l'écrit par défaut depuis la version 0.4. L'ancien format non préhaché est refusé par
  l'hôte, quel que soit le signataire.
- **Aucune clé ne traverse le produit.** La clé secrète ne quitte jamais la machine de
  l'hébergeur ; le produit ne contient ni clé secrète, ni clé publique préconfigurée.

Exemple de cycle de publication :

```sh
# 1. écrire le nouveau document (version montante, expires à <= 90 jours)
$EDITOR index.json
# 2. signer au format préhaché
minisign -S -m index.json
# 3. vérifier soi-même
minisign -V -p cle-publique.pub -m index.json
```

Le commentaire de confiance doit redire la `version` et l'`expires` du document
(`shelfhold-index v1 version=… expires=…`).

## Rotation de clé

Pour changer de clé sans perdre les administrateurs déjà épinglés :

1. Générer une nouvelle paire minisign.
2. Poser `next_pubkey` (le blob public de la nouvelle clé) dans le document.
3. Signer le **même** document avec l'ancienne clé (`index.json.minisig`) **et** la nouvelle
   (`index.json.next.minisig`), même commentaire de confiance.
4. Publier les trois fichiers. L'hôte déplace l'épingle à la clé suivante quand il voit les
   deux signatures. Les plugins déjà installés restent épinglés à leur signataire d'origine ;
   la clé publique de leur ligne est mise à jour pour suivre la rotation.

Une rotation n'est jamais une prise de contrôle par un tiers : un document signé par une autre
clé que celle épinglée est refusé.

## Critères d'admission (ENF-LEG-003)

Un index qui vise la confiance d'un foyer doit exiger de chaque entrée :

- preuve de licence ou d'autorisation de la source amont ;
- conditions et fichier robots lus et respectés ;
- limites déclarées (`rate_limit`, `quota`) et aucun secret dans le manifeste ;
- kit de conformité vert, y compris en mode isolé ;
- revue par les mainteneurs (CODEOWNERS) ;
- aucune dépendance à une API interne de Shelfhold.

L'index officiel et un index tiers appliquent les mêmes critères. Il n'y a **aucune liste de
sites** dans le dépôt : l'index est une liste de plugins et de roues, pas de sources amont, et
l'administrateur décide de ce qu'il épingle.

## Révocation

- `revoked.plugin_keys` : clés de plugins retirées ; une révocation, une fois vue, reste vue,
  même si un index suivant l'omet.
- `revoked.wheel_sha256` : roues retirées.
- Un plugin révoqué n'est plus installable ; une roue révoquée n'est plus téléchargeable.
- `deprecated` et `replaced_by` ne sont pas des révocations : le plugin reste installable et
  mis à jour, et la mention est lue par l'administrateur.

## Ce que l'hôte vérifie à chaque rafraîchissement

- signature préhachée, clé épinglée, commentaire de confiance ;
- expiration (passée ou à plus de 90 jours : refus) ;
- version monotone (un index rejoué est refusé) ;
- deux index pour un même numéro : refus ;
- rotation par double signature ;
- empreinte sha256 de la roue avant que le fichier ne soit vu par qui que ce soit d'autre.
