# Importer une sauvegarde Mihon (`.tachibk`)

Une sauvegarde Mihon dit quelles séries vous suiviez, quels chapitres vous aviez lus et à quels suivis (AniList, etc.) elles
étaient liées. Shelfhold la lit, vous montre ce qu'il en tirerait, puis l'écrit quand vous le décidez.

C'est une **action réservée** de l'administrateur : une session récente, ou la ligne de commande de l'hôte. Jamais une clé d'API
(même en lecture seule), jamais un membre ni un enfant.

## Deux étapes

1. **Aperçu.** `shelfhold import tachibk sauvegarde.tachibk [--profile prénom]`, ou `POST /api/v1/imports/tachibk` avec le fichier
   pour corps. Le fichier est lu, puis Shelfhold dit combien de séries, de chapitres, de chapitres prouvés lus, de chapitres
   commencés et d'entrées de suivi il contient. **Rien n'est écrit dans la bibliothèque.**
2. **Application.** `shelfhold import tachibk --apply <identifiant>`, ou `POST /api/v1/imports/tachibk/{id}/apply`. Un aperçu ne
   s'applique qu'une fois ; réimporter le même fichier ne crée aucun doublon.

## Ce que l'import écrit

| Dans la sauvegarde | Dans Shelfhold |
|---|---|
| Une série de la bibliothèque Mihon | Un **élément en attente** (`mihon:<numéro de source>`) : sa source n'est pas résolue à l'import |
| Les chapitres qu'elle liste | Des unités **connues** (`known`), dans la fiche de l'élément en attente |
| Un chapitre marqué lu | Un **marqueur de lecture** du profil choisi |
| Un chapitre commencé (une page) | Un marqueur avec la page, jamais « lu » |
| Une entrée de suivi (AniList, MyAnimeList, Kitsu, MangaUpdates, Shikimori, Bangumi) | Un **indice de suivi** : le suivi, l'identifiant, le dernier chapitre |

Les marqueurs vont au **profil choisi** (`--profile`, `profile_id`) et à lui seul. Par défaut, c'est le vôtre ; en ligne de commande
sur l'hôte, c'est le premier administrateur.

## Ce que l'import ne fait jamais

- **Appeler une adresse du fichier.** Les adresses de séries et de chapitres sont gardées comme du texte ; la résolution des
  sources passe plus tard par le Fetcher et sa garde.
- **Marquer « lu » plus que le fichier ne prouve.** Un suivi qui dit « chapitre 40 », ou un chapitre ultérieur lu, ne rend aucun
  autre chapitre lu. Un marqueur ne revient jamais en arrière quand on réimporte.
- **Garder un nom de source.** Une source n'est que son numéro ; les noms ne sont ni lus, ni journalisés, ni exportés.
- **Garder un secret.** Les préférences de la sauvegarde (qui portent les jetons de suivi) ne sont jamais lues ; un indice de
  suivi ne garde ni adresse, ni titre.
- **Toucher à la bibliothèque, aux positions de lecture ou aux étagères.** Un marqueur n'est pas une position de lecture.

Les séries hors bibliothèque (non favorites) et celles de la source locale (un dossier de l'appareil) sont laissées de côté et
comptées.

## Un fichier hostile est refusé en entier

Le fichier est décodé dans un sous-processus borné (sans réseau, mémoire et durée limitées), sans nouvelle dépendance. Il est
refusé **en entier** (422, `import.tachibk_invalid`, avec un mot de raison, jamais un morceau du fichier) au-delà de :

| Mot | Borne |
|---|---|
| `too_large` | 16 Mio compressés |
| `decompressed_too_large` | 64 Mio décompressés (bombe gzip) |
| `too_deep` | profondeur 16 |
| `too_many_entries` | 100 000 champs lus |
| `malformed`, `not_gzip`, `empty` | fichier abîmé, pas une sauvegarde, vide |

Les champs inconnus sont ignorés, et une série dont l'adresse ou le titre dépasse 2 048 octets est écartée (comptée dans l'aperçu), pas le fichier. Le format n'a pas de numéro de version : Shelfhold reconnaît les générations (`mihon-1` à `mihon-3`) à leurs champs de premier niveau. Un champ de premier niveau qu'il ne connaît pas (ceux des forks, ou d'une version plus récente) est ignoré et signalé dans l'aperçu (`unknown_fields`) ; seul un fichier sans aucun champ connu est refusé (`import.tachibk_schema`).
