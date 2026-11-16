# La vue `reading_progress`

Contrat lu par le relais kosync (L47, L50), la synchronisation avec Kavita (L14b), le suivi, la
révision et la mise à niveau. **Colonnes figées depuis la 1.1** : un ajout est possible, un retrait
ou un changement de sens attend la 2.0. Aucune écriture : un lecteur enregistre une position
(`reading.public.record_position`) et la vue est recalculée.

| Colonne | Type | Sens |
|---|---|---|
| `user_id` | ULID | le profil ; chaque profil a sa propre progression |
| `unit_id` | ULID | l'Unité |
| `state` | `unread`, `in_progress`, `read` | l'état le plus avancé atteint |
| `last_position_id` | ULID | la position **la plus avancée** pour cette Unité |
| `latest_position_id` | ULID | la **dernière** position écrite (`client_ts` borné par `server_ts`) |
| `approximate` | entier 0 ou 1 | 1 : proportion, ou position prise sur un contenu remplacé |

## Quelle position lire

- **Où le lecteur est arrivé le plus loin** (réponse à une liseuse qui demande la position, ADR-0053
  §5, « une seule règle : la plus avancée ») : `last_position_id`, avec le `timestamp` de cet
  enregistrement, jamais « maintenant ».
- Le `GET /kosync/syncs/progress/:document` (L47) lit `last_position_id`, mais ne la rend que dans le
  repère du **document demandé** : sur le même fichier, ou retraduite par `bundle_member` pour un
  bloc ; une position tenue sur un autre fichier de l'Œuvre que l'on ne sait pas traduire n'est pas
  rendue, et une plus ancienne ne la remplace pas.
- **Où le lecteur en est maintenant** (reprise, « continuer la lecture »), même s'il est revenu en
  arrière : `latest_position_id`.

## La règle

1. Une position est projetée sur chaque Unité que son fichier couvre : exacte si la page (un
   **entier**) et les bornes de page de la couverture sont connues, sinon proportionnelle au rang
   de l'Unité dans le fichier et `approximate` (sauf fichier à une seule Unité).
2. Une position prise sur un **contenu remplacé** n'utilise jamais sa page et n'atteint que les
   Unités que **son** contenu couvrait (`covered_units`, la couverture au moment de l'écriture, ou
   celle d'une position antérieure sur ce même hash si elle arrive après le remplacement) ; elle
   est toujours `approximate`. Sans couverture connue de cet ancien contenu, elle n'atteint rien,
   jamais la couverture actuelle.
3. Un rapport de **liseuse ou de lecteur** (`source = device`) ne dit jamais `read` à lui seul : un
   coup d'œil à la dernière page est une position, pas une lecture. `read` vient d'une **traversée
   plausible** : une Unité est lue quand les déplacements plausibles entre positions successives du
   profil sur le même contenu la couvrent en entier (de ses premières pages, ≤ 3, à sa dernière
   page). Un déplacement vers l'avant de A à B est plausible s'il fait au plus 3 pages (toujours
   admis), ou si `pages / (server_ts(B) − server_ts(A))` reste sous `reading.max_pages_per_minute`
   (20 par défaut). Seule la date du **serveur** compte : le `client_ts` n'entre jamais dans la
   vitesse. KOReader ne pousse pas à chaque page (`auto_sync` éteint et `pages_before_update = nil`
   par défaut, `plugins/kosync.koplugin/main.lua`) : **sans `auto_sync`, la liseuse n'envoie rien d'elle-même**
   (ni à l'ouverture, ni à la fermeture, ni à la mise en veille) ; avec lui, elle demande la position à
   l'ouverture et au réveil, et l'envoie à la fermeture, à la mise en veille et périodiquement. Une
   position arrive donc un chapitre plus loin, et l'ancienne règle (3 pages) ne marquait rien.
   Dans le repère d'un **bloc de série**, le point de départ est celui du **même appareil**
   (`device_id`, sinon son jeton) : jamais la position d'un autre. Une position qui renvoie la page
   que la lecture (`GET`) vient de donner à ce jeton, ou une page plus tôt du même chapitre, est un
   point de départ et non une lecture (la liseuse revient là où on l'a envoyée) : elle ne marque
   rien. La dernière page donnée est gardée en mémoire (bornée, perdue au redémarrage).
   Le point de départ A est la dernière position connue du profil sur ce contenu, sinon la
   **remise du fichier** au profil (page 0, `record_delivery`), sinon rien : un premier rapport
   sans point de départ n'atteint rien. Fichier sans bornes de page et à une seule Unité : mêmes
   règles en parts du fichier (5 % admis ; au-delà, la vitesse en pages demande le nombre de
   pages du contenu). La marque est journalisée dans `reading_position.read_units` et le membre
   peut l'annuler (`undo_read_mark`) ; une marque annulée ne revient que sur de nouveaux
   déplacements.
   Un état `server` (Kavita) est pris tel quel, **annulable aussi** : l'annulation est une
   surcouche (`undone_units`) qui l'emporte jusqu'à ce qu'une position plus récente aille
   au-delà.
4. Par profil et Unité, la plus avancée gagne : l'état, puis exacte avant approximative, puis **la
   fraction de l'Unité atteinte** (jamais le pourcentage de deux fichiers différents), puis la date,
   puis l'id. Une Unité lue ne repasse jamais à « non lue ».
5. Aucune Unité qu'aucune position n'atteint n'est marquée : un saut en avant ne marque lu aucun
   chapitre sauté.

## Changement d'Édition préférée

`POST /api/v1/editions/{edition_id}/preferred` (administrateur ; une clé en lecture seule est
refusée) fait de l'Édition la préférée de son Œuvre, puis reporte la lecture de **chaque profil**
dans la nouvelle Édition (`reading.application.transfer`). La réponse compte les profils
(`transferred`, `pending`), jamais lesquels.

- **Position neutre** : `work_id`, `part_ordinal` (rang de la partie atteinte), `total_progression`
  (0 à 1 sur l'Œuvre), `completed`, `approximate = true`, plus le numéro de la partie
  (`part_number`) et l'avancement dans cette partie (`part_progression`). La partie atteinte est
  la dernière commencée dans l'ordre de lecture de l'ancienne Édition ; un tome lu à la page 143
  est déjà un chapitre et une part de ce chapitre, par les bornes `start_locator` / `end_locator`
  de `unit_coverage`.
- **Correspondance** : la même numérotation désigne la même partie ; sans elle, la part du tout
  décide. L'Œuvre finie arrive à la dernière partie, entière.
- **Écriture** : une position `transposed` (`locator.transposed = true`, `device_id = "transfer"`)
  sur le fichier de la nouvelle Édition qui porte l'Unité, sans page, avec une part du fichier. La
  vue la dit toujours `approximate` (0084), ainsi que les Unités qu'elle journalise en `read_units`.
  Une Unité n'est journalisée lue que si l'ancienne Édition la disait lue (même numéro) : jamais
  un chapitre sauté. La marque reste annulable (`undo_read_mark`).
- **Un seul critère de « lu » qui sort** : la vue `reading_progress_proven` (mêmes colonnes, sans les positions transposées). Suwayomi, Kavita (page « lue » de `_all_read`) et le `GET` kosync la lisent ; une marque transposée reste visible et annulable dans `reading_progress`, jamais poussée.
- **Même genre seulement** : un numéro ne relie que des Unités de même `kind` ; une Unité couverte en partie (`extent`) n'est jamais reportée « lue ».
- **Jamais une preuve** : une position transposée n'est pas l'ancre d'une traversée, n'est jamais
  une page à pousser vers Kavita, et ses marques ne sont jamais relayées comme « lu ».
- **Portée** : un profil qui ne voit pas la nouvelle Édition (enfant au-dessus de son plafond), ou
  qui n'a rien lu, ne reçoit rien ; l'ancienne progression est gardée. Une partie sans fichier
  n'invente rien : le profil est compté `pending`.
- Rappeler la même demande n'ajoute rien.

## Écrire

- `record_position` normalise la page à l'écriture (entier ; un nombre en texte ou un flottant
  entier devient entier ; le reste lève `InvalidLocatorError`, un 422 pour les routes) et la base
  refuse tout autre type (`CHECK`).
- `record_position` n'a **pas** d'argument `source` : c'est toujours un rapport d'appareil
  (`device`). L'état qu'un serveur tient lui-même s'écrit par une entrée distincte et interne,
  `reading.application.server_state.record_server_state`, que ni les routes, ni la ligne de
  commande, ni un jeton d'appareil n'importent (contrat import-linter et test d'architecture) ;
  elle exige un compte membre lié et vivant (`live_member_account`, `revoked_at` relu à chaque
  appel) et note la destination (`device_id = "server:<destination>"`).
- Contrat pour L14b (envoi et tirage Kavita, écrit dans `docs/reference/progress-sync.md`) : Shelfhold **note ce qu'il envoie** à Kavita ; l'écho
  de son propre envoi n'est **jamais relu** comme un « lu » `server` ; la dernière page d'un
  chapitre n'est envoyée comme lue que si une traversée a marqué l'Unité, sinon Kavita reçoit la
  page, pas la marque.
- Le lecteur vient du principal authentifié, jamais de la requête ; un enfant ne voit ni
  n'enregistre rien au-dessus de son plafond.

## Coût

Mesuré sur 10 000 positions (500 fichiers de 10 Unités, un profil, SQLite sur ssp) : la vue entière
ou filtrée par profil en 0,9 s ; une Unité d'un profil en 45 ms (le filtre est poussé dans la
vue). Une liseuse interroge Unité par Unité : sous le seuil. Au-delà de ~100 000 positions, prévoir
un filtre par profil systématique ou une table dérivée.

## Alias de document

`heal_alias_hashes(engine, locate=…)` remplit les empreintes KOReader et Kavita qu'un arrêt brutal
a laissées vides. Contrat de `locate(export_item_id, file_hash)` : un chemin **sous les racines
physiques** de la liaison, jamais un lien symbolique (résoudre et refuser ce qui sort des racines),
ou `None`. Le fichier est ouvert une fois et lu **une fois** : le SHA-256 et les échantillons des deux
empreintes sortent de la même passe (`verified_hashes`), les octets échantillonnés sont donc ceux
qui ont été vérifiés, sans comparaison de taille ni de date. Il doit être celui que l'alias nomme, et l'écriture ne remplit que des empreintes encore vides.
