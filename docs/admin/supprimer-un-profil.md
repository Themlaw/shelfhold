# Supprimer un profil, exporter ses données

Deux gestes réservés à l'administrateur (EF-USR-013) : une session d'admin **fraîche** (ou la CLI de l'hôte), jamais une clé d'API, jamais un membre ni un enfant.

```sh
shelfhold profile export <nom> > export-<nom>.json   # ses données, en JSON, sans aucun secret
shelfhold profile remove <nom>                       # la suppression
```

Par l'API : `GET /api/v1/profiles/{id}/export` et `DELETE /api/v1/profiles/{id}`. Le dernier administrateur ne peut pas être supprimé, ni un administrateur par lui-même.

## Ce que contient l'export

Le profil et ses préférences, ses politiques par source, son état par Œuvre (suivi, favori, lu), ses demandes, ses tickets de suivi et de téléchargement, ses positions de lecture, ses sessions (avec adresse IP et agent), ses clés (préfixe, nom, droits), ses clés d'accès (nom, dates) et les lignes d'audit de ses actes.

Il ne contient **jamais** : le hash du mot de passe, un jeton ou un secret de session, le hash d'une clé, le matériel d'une clé d'accès, ni aucun titre d'Œuvre (une Œuvre y est nommée par son identifiant : l'export d'un enfant ne peut pas porter un titre qu'il ne verrait pas). Les identifiants des autres profils, dans les lignes d'audit d'un administrateur, sont remplacés par `other-1`, `other-2`… Les clés émises pour un enfant sont dans l'export de l'enfant, pas dans celui de l'administrateur.

Chaque liste s'arrête à 20 000 lignes ; la clé `truncated` nomme les sections coupées (jamais en silence). La réponse n'est pas mise en cache et part en pièce jointe. L'export écrit une ligne d'audit (`profile.exported`) même en mode maintenance : c'est la seule écriture d'une action en lecture, elle ne contient jamais le contenu.

## Ce que la suppression fait

En une transaction : le profil, ses sessions, ses clés, ses clés d'accès, ses politiques, son état par Œuvre, ses demandes, ses positions de lecture et ses comptes de source disparaissent. L'audit **n'est pas supprimé** : l'identifiant est remplacé, partout où il figure, par un jeton opaque `deleted:<hex>` tiré au hasard (sans lien avec l'identifiant), le même pour toutes les lignes d'une même personne. Les événements qui la citent, comme acteur ou dans leur contenu, sont effacés, et les jetons `?since=` des clients sont coupés (ils rechargent leurs listes). Ses tickets de suivi et de téléchargement restent pour l'administrateur, sans propriétaire et avec leur visibilité d'origine : un ticket privé reste masqué. La décision d'un admin supprimé sur une demande, la pause de file qu'il a posée, le renoncement à un trousseau perdu qu'il a saisi et les résumés en attente qui le citent prennent le même jeton.

## Ce que la suppression n'atteint pas

Dites-le à la personne concernée avant de supprimer.

- **Les sauvegardes déjà faites** (14 par défaut, `backup_retention`) : elles contiennent encore le profil, et son identifiant en clair dans l'audit. Elles vieillissent avec la rétention ; pour hâter, supprimez les anciennes archives de `backups/`. Une restauration d'avant la suppression ramène le profil sans mot de passe ni clé d'accès (il faut le recréer, ou le supprimer à nouveau).
- **Les journaux du conteneur** (Docker) : la journalisation HTTP écrit le chemin (`/api/v1/profiles/<id>/export`, `/<id>`) et certains événements de sécurité écrivent l'identifiant. Ils tournent selon la politique de votre hôte ; purgez-les si le délai ne vous convient pas.
- **Le fichier d'export** que vous venez de produire : il est à vous de le protéger ou de le détruire.
- **La bibliothèque** : les Œuvres, les fichiers et les classements qu'elle a posés restent (ils sont au foyer).
- **Kavita et les autres services connectés** : le compte Kavita lié n'est pas touché (lecture seule, EF-USR-017).
- **Les 90 jours d'événements et l'année d'audit** sont une exemption domestique documentée (grille W1) : l'audit garde ses lignes sous le jeton jusqu'à son échéance, puis elles partent comme les autres.
