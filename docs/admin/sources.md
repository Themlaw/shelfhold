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
nomment la source, le champ et une génération qui augmente à chaque écriture : une valeur recopiée
sur une autre source ou un autre champ, ou une ancienne valeur remise en place, ne se déchiffre
pas. Un secret de moins de 8 caractères est refusé : il serait trop court pour être masqué. Une sauvegarde contient ces valeurs chiffrées ; une restauration les relit avec le
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

## Project Gutenberg, libre de droits aux États-Unis (catalogue officiel et miroir)

Gutenberg publie des œuvres **libres de droits aux États-Unis**. Ce n'est pas forcément le cas dans
votre pays : voir [domaine-public.md](domaine-public.md). Shelfhold garde la mention « domaine public
aux États-Unis » telle quelle sur l'Édition, calcule l'avertissement pour le pays de l'instance et
n'en fait jamais une affirmation sur votre pays.

La source ne lit **jamais les pages du site**. Elle prend :

- **le catalogue officiel** (un fichier CSV ou RDF des flux officiels, chemin `/cache/epub/feeds/`),
  à l'adresse du réglage `catalog_url`. Il est lu au plus une fois par semaine, avec une requête
  conditionnelle (`ETag` / `If-Modified-Since`) ;
- **les fichiers d'un miroir** de la liste officielle, que vous choisissez : réglage `mirror_url`,
  obligatoire. Un miroir n'est jamais le site lui-même. Une **liste blanche figée** de miroirs
  officiels (`aleph.gutenberg.org`, `gutenberg.pglaf.org`) est reconnue dans le code, y compris sous le
  nom du projet : tout autre nom en `gutenberg.org` ou `gutenberg.net`, dont `www.gutenberg.org` et
  `gutenberg.org`, reste refusé comme miroir. La liste ne se modifie pas par un réglage.
  `aleph.gutenberg.org` est cité par le guide officiel comme point rsync ; au 2026-11-12 il répond aussi en
  HTTP, mais son certificat HTTPS ne porte que le nom `aleph.pglaf.org` : une adresse `https://aleph.gutenberg.org`
  est donc refusée à la vérification TLS. Utilisez `http://aleph.gutenberg.org/…` ou `https://gutenberg.pglaf.org/…`
  (les EPUB restent traités comme non fiables dans les deux cas).

Aucun hôte n'est écrit dans le code : sans ces deux réglages, la source n'est pas configurée
(`source.not_configured`) et n'envoie rien. Les règles de domaine ne laissent passer que ces deux
adresses ; si le catalogue est servi par le site, seul le chemin des flux officiels est ouvert sur cet
hôte, et un miroir qui redirige vers une page du site est refusé à ce saut.

Le rythme est lent et tenu par Shelfhold pour toute la source : une requête toutes les 5 secondes
au plus, quel que soit le nombre de demandes en cours (les lectures de `robots.txt` et chaque saut de
redirection comptent). Les EPUB du miroir sont toujours **non fiables** : ils passent par le
neutralisateur avant tout export, et l'original reste dans `/store`.

Pour l'activer : réglez `catalog_url` et `mirror_url` avec `shelfhold plugin set` sur la clé de la
source, puis ajoutez cette clé aux sources actives. La liste des cibles de canari
(`tools/canary_targets.toml`) est vide et ne vise jamais le site du projet.

## Source OPDS (Kavita, Komga, Calibre-web…)

La source `opds` lit un catalogue OPDS 1.2 (Atom) ou 2.0 (JSON), avec la recherche OpenSearch du catalogue.

- `base_url` : la racine du catalogue, en `http(s)://hôte/…`. Pas d'identifiants dans l'adresse : ils vont dans les secrets. Pour Kavita, la clé fait partie du chemin : `https://kavita.example/api/opds/{apiKey}` avec `auth = api_key_path`.
- `auth` : `none`, `basic` (`username` et le secret `password`), `api_key_header`, `api_key_query` ou `api_key_path` (le secret `api_key`). Les identifiants ne partent que vers l'origine de `base_url`, jamais en suivant un lien ou une redirection vers un autre hôte.
- Seul l'hôte de `base_url` est joignable ; un lien de téléchargement ailleurs n'est pas proposé.
- Les EPUB reçus sont **non fiables** : ils passent par le neutralisateur avant tout export.
- **Si le catalogue est l'instance d'une destination active** (le Kavita où Shelfhold écrit), les réglages affichent « utilisez readback » : Shelfhold ne réimporte jamais ses propres exports. Un fichier qui en est un est rangé comme réplique de l'export, jamais comme nouvelle Variante.

## Wikisource, un EPUB par livre avec son attribution CC BY-SA

La source `wikisource` prend **un livre** (une page de l'espace principal d'un Wikisource) et en
récupère l'EPUB auprès de l'outil d'export du wiki. Elle ne lit jamais une page du wiki : les règles
de domaine ne laissent passer que l'API (`/w/` d'un hôte `*.wikisource.org`) et l'outil d'export,
et toute autre adresse est refusée avant d'envoyer quoi que ce soit.

- Chercher : par titre, dans la langue du réglage `language` (par défaut `fr`) ou du filtre
  « Langue ». Un code qui ne ressemble pas à un code de langue est refusé.
- Coller l'adresse d'un livre (`https://fr.wikisource.org/wiki/Titre`) donne le livre, sans requête.
- Rythme : une requête par seconde au plus pour toute la source, quel que soit le nombre de
  demandes en cours.
- Les EPUB sont toujours **non fiables** : le neutraliseur passe avant tout export, l'original est
  gardé dans `/store`.

Le texte de Wikisource est partagé sous **CC BY-SA** : la licence demande que les contributeurs, la
licence et un lien vers l'origine accompagnent le texte. Shelfhold construit cette attribution
(le lien de la page, la licence, les contributeurs de la page, en nombre borné), la range sur
l'Édition avec sa provenance (la source qui l'a dite) et l'affiche sur la fiche. À l'export, elle est
copiée dans l'EPUB (`dc:rights` et `dc:source`) quand le fichier n'a pas de `dc:rights` ; s'il en a un
qui ne nomme pas la licence, la ligne du livre reste telle quelle et l'attribution est écrite à côté.
Un nom de contributeur qui contient du balisage, un lien ou un saut de ligne n'est pas recopié.

## Standard Ebooks, libre de droits aux États-Unis (une adresse par livre, flux patron)

Standard Ebooks publie des éditions soignées d'œuvres **libres de droits aux États-Unis**, ce qui
n'est pas forcément le cas dans votre pays : voir [domaine-public.md](domaine-public.md). La
production est versée au domaine public (CC0) ; Shelfhold garde la mention « libre de droits aux
États-Unis » telle quelle sur l'Édition et n'en fait jamais une affirmation sur votre pays.

Deux entrées, et pas de troisième :

- **Une adresse par livre.** Coller l'adresse d'un livre (`…/ebooks/auteur/titre`)
  donne le livre, sans requête ; le fichier est le téléchargement de ce livre, une seule requête. Le
  titre vient de l'adresse. Shelfhold ne lit **jamais** une page du site, une liste d'auteurs ou de
  sujets : il ne moissonne pas.
- **Le flux OPDS des patrons.** Le flux complet est réservé aux patrons du projet. Il n'est
  lu que si vous avez réglé `auth` sur `basic` avec votre identifiant de patron (`username`, et le
  mot de passe comme secret : [secrets.md](secrets.md)). Sans cela, aucune requête ne part vers le
  flux, quoi qu'on demande. Le flux est lu par la source OPDS générique ; une recherche lit une page
  de résultats et rend un curseur, elle ne suit jamais la page suivante d'elle-même.

L'identifiant est injecté par l'hôte, vers `standardebooks.org` seulement (aucun réglage ne change
cette origine), et masqué des journaux, du cache, des références et du paquet de diagnostic. Le
rythme est lent (une requête toutes les 5 secondes au plus pour toute la source) et les EPUB sont
toujours **non fiables** : le neutraliseur passe avant tout export, l'original est gardé dans
`/store`. Pour l'activer, ajoutez la clé `standardebooks` aux sources actives.
