# Historique d'activité, chapitres bloqués, stockage et réseau

Trois écrans pour l'administrateur, et pour lui seul : une session d'administrateur, ou une clé de portée `admin`. Un enfant, un
membre, une clé en lecture seule (même celle de l'administrateur), un jeton d'appareil ou une clé de demande reçoivent un refus
(403) avant que quoi que ce soit soit lu.

## Historique d'activité (`/activite/historique`)

L'historique est la page d'activité sans sa fenêtre : mêmes événements, même filtre (la visibilité de chaque événement et le
plafond de la personne), lus du plus récent au plus ancien, par pages de 50. Il ne garde de chaque événement qu'une liste fermée
de champs (nombres, codes, identifiants de vos propres objets) : un chemin, un nom d'hôte, une adresse IP ou une clé qu'un
événement aurait portée n'est jamais montré.

## Chapitres bloqués

Un chapitre bloqué est désigné par ce que sa source l'appelle, `(source, identifiant chez la source)`. Quand la source le
repropose, le cycle de suivi l'ignore : aucune Unité, aucune demande, aucun fichier. Chaque fois, le bloc compte la
proposition ignorée, ce que l'écran montre (« proposé de nouveau 3 fois et ignoré »).

- **Rien ne débloque en silence.** Ni le temps, ni un nouveau cycle, ni la suppression de la série ou du compte qui a bloqué.
  Seul le bouton « Débloquer » le fait, en deux temps (la question, puis « Confirmer le déblocage »), et l'action est inscrite
  dans l'audit (`blocklist.remove`, comme `blocklist.add` pour le blocage).
- Le blocage vaut pour la suite : un chapitre déjà reçu reste où il est, et une demande déjà ouverte va à son terme.
- Raison : une liste fermée (mauvaise qualité, mauvais contenu, doublon, autre), jamais un texte libre.
- L'écran montre le chapitre par le titre de l'Œuvre et son numéro quand ils sont connus ici ; l'identifiant de la source, qui
  peut être une adresse chez elle, n'est jamais affiché (une empreinte courte seulement, dans l'API).
- Bloquer passe par l'API : `POST /api/v1/ops/blocklist` avec `source_key`, `reason` et soit `remote_unit_id`, soit `unit_id`
  (l'Unité de Shelfhold dont la source a proposé le chapitre).

## Stockage et réseau (`/sante/stockage`)

- **Espace par étagère** : la taille et le nombre de copies que le registre des copies tient sur vos disques (magasin,
  bibliothèque, destinations), par étagère, plus ce qui n'est rangé sur aucune. Un même fichier lié deux fois compte une fois ;
  une copie trouvée abîmée ne compte pas ; l'origine (la source) n'est pas sur vos disques.
- **Trafic par source** : les requêtes sorties de la machine et les octets reçus, par source et par jour (UTC), sur 7, 30 ou
  90 jours. Le compteur est posé dans le Fetcher **après** la garde réseau : une requête que la garde refuse n'est pas du trafic,
  une réponse servie par le cache non plus. Il ne garde ni adresse, ni hôte, ni chemin, ni requête : une source, un jour, deux
  nombres.

Aucun de ces écrans n'ajoute quoi que ce soit au paquet de diagnostic.
