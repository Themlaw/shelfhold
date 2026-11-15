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
- **Où le lecteur en est maintenant** (reprise, « continuer la lecture »), même s'il est revenu en
  arrière : `latest_position_id`.

## La règle

1. Une position est projetée sur chaque Unité que son fichier couvre : exacte si la page (un
   **entier**) et les bornes de page de la couverture sont connues, sinon proportionnelle au rang
   de l'Unité dans le fichier et `approximate` (sauf fichier à une seule Unité).
2. Une position prise sur un **contenu remplacé** n'utilise jamais sa page et n'atteint que les
   Unités que le fichier couvrait **quand elle a été écrite** (`covered_units`) ; elle est toujours
   `approximate`. Sans cette liste (ligne ancienne), elle n'atteint rien.
3. Un rapport de **liseuse ou de lecteur** (`source = device`) ne dit jamais `read` à lui seul : un
   coup d'œil à la dernière page est une position, pas une lecture. `read` vient d'une **traversée
   page à page** : une chaîne de positions du même appareil, sur le même contenu, des premières
   pages (≤ 3) d'une Unité à sa dernière page puis à la première page de la suivante, sans écart de
   plus de 3 pages entre deux positions successives (fichier sans bornes de page et à une seule
   Unité : mêmes règles avec des écarts de 5 % du fichier). La marque est journalisée dans
   `reading_position.read_units` et le membre peut l'annuler (`undo_read_mark`) ; une marque annulée
   ne revient que sur de nouvelles positions. Un état `server` (Kavita) est pris tel quel.
4. Par profil et Unité, la plus avancée gagne : l'état, puis exacte avant approximative, puis **la
   fraction de l'Unité atteinte** (jamais le pourcentage de deux fichiers différents), puis la date,
   puis l'id. Une Unité lue ne repasse jamais à « non lue ».
5. Aucune Unité qu'aucune position n'atteint n'est marquée : un saut en avant ne marque lu aucun
   chapitre sauté.

## Écrire

- `record_position` normalise la page à l'écriture (entier ; un nombre en texte ou un flottant
  entier devient entier ; le reste lève `InvalidLocatorError`, un 422 pour les routes) et la base
  refuse tout autre type (`CHECK`).
- `source = "server"` est réservé à la lecture de l'état qu'un serveur tient lui-même ; un rapport
  d'appareil reste `device`.
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
ou `None`. Le fichier est ouvert une fois (SHA-256 et empreintes sur le même descripteur), doit
être celui que l'alias nomme, et l'écriture ne remplit que des empreintes encore vides.
