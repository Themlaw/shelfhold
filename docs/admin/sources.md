# Réglages d'une source

Chaque source décrit ses réglages dans son manifeste (`settings_schema`, un petit schéma JSON de
champs plats : texte, nombre, booléen, liste de valeurs). Shelfhold le vérifie au chargement du
plugin et refuse un plugin dont le schéma sort de ce cadre (EF-SRC-013).

## Valeurs, secrets et cookies

- Une **valeur ordinaire** (langue, taille de page…) se lit et se change librement :
  `shelfhold plugin settings <clé>`, `shelfhold plugin set <clé> lang=en`,
  `GET` et `PATCH /api/v1/sources/<clé>/settings`.
- Un **secret** est un champ marqué `writeOnly: true` ou `format: password`. Il se règle à part,
  lu dans un fichier ou sur l'entrée standard, jamais sur la ligne de commande :
  `shelfhold plugin set-secret <clé> <champ> --secret-file <fichier>`, ou
  `PUT /api/v1/sources/<clé>/secrets/<champ>`.
- Les **cookies** d'une source (un objet JSON nom → valeur) se remplacent d'un bloc :
  `shelfhold plugin set-cookies <clé> --cookies-file <fichier>` (`{}` les efface), ou
  `PUT /api/v1/sources/<clé>/cookies`.

Changer un secret ou des cookies est une **action réservée** (`source.set_secret`) : session fraîche
ou CLI locale, jamais une clé d'API, et une ligne d'audit (`source.secret_set`,
`source.secret_cleared`, `source.cookies_set`) qui nomme la source et le champ, jamais la valeur.

## Ce qui ne relit jamais un secret

Ni l'API ni la CLI ne rendent un secret : elles disent « défini » ou « non défini » et donnent une
**empreinte courte** (8 caractères, calculée avec une clé tirée du trousseau), qui permet de vérifier
qu'une valeur a changé sans la montrer.

Au repos, chaque secret et le pot de cookies sont **chiffrés un par un** dans la table
`source_secret`, avec l'enveloppe du trousseau ([secrets.md](secrets.md)). Les données associées
nomment la source et le champ : une valeur recopiée sur une autre source ou un autre champ ne se
déchiffre pas. Une sauvegarde contient ces valeurs chiffrées ; une restauration les relit avec le
trousseau restauré (ou `SHELFHOLD_SECRET_KEY_FILE`).

Dès qu'un secret est déchiffré, Shelfhold l'ajoute à la liste des valeurs à masquer, sous toutes ses
formes (encodée dans une adresse, `nom=valeur`, en-tête `Cookie`). Il n'apparaît donc ni dans les
journaux, ni dans le paquet de diagnostic, ni dans le cache HTTP, ni dans une référence enregistrée,
ni dans une réponse d'erreur de l'API.

## Réglages réservés

Le proxy par source, le solveur anti-robot, l'imitation d'empreinte TLS et le navigateur sans tête
sont **réservés** : aucun plugin ne peut les déclarer en M2. Ils viendront avec la première source
native qui en aura besoin, toujours derrière la même garde contre les requêtes internes.

Le proxy par source sert au **routage réseau du foyer** (passer par la sortie réseau choisie pour
la maison), rien d'autre. Shelfhold ne le présente jamais comme un moyen de joindre un site qui ne
répond pas chez vous, et la documentation est vérifiée en continu sur ce point.
