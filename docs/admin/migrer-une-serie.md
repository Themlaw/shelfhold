# Migrer une série vers une autre source

Quand une source disparaît ou qu'une autre fait mieux, une série peut passer à la nouvelle source
**sans rien retélécharger** : les fichiers, l'historique et la progression de lecture restent là. La
nouvelle source numérote parfois autrement (ses chapitres 11, 12… pour les 1, 2… de la bibliothèque) :
la migration range la correspondance entre les deux numérotations.

## Les trois temps

1. **Proposer.** Sur la fiche de la série, « Migrer » (visible quand une liaison est inactive) ouvre
   l'écran. Tu donnes la nouvelle source et l'identifiant de la série chez elle. Shelfhold lit ce
   qu'elle liste et propose une table : pour chaque numéro de la source, le numéro de la bibliothèque
   et la raison (même numéro, décalage constant, nouveau). **Rien n'est écrit.**
2. **Valider.** Tu peux changer une ligne. Une collision (deux numéros de la source sur un même numéro
   de la bibliothèque) est refusée. La validation fige la table.
3. **Appliquer.** L'ancienne liaison est retirée, la nouvelle prend la place. Les numéros déjà rangés
   ne sont ni retéléchargés ni dupliqués ; seuls les numéros que la bibliothèque n'a pas sont
   demandés. La table n'est appliquée que si son empreinte est celle que tu as validée : si la source a
   changé entre-temps, la migration est refusée (`source_migration.stale`) et il faut reproposer.

La progression et l'historique de lecture sont gardés, et aucun chapitre n'est marqué « lu » à ta place.
Une coupure en plein milieu (`kill -9` compris) reprend sans doublon ni perte : l'application tient en
une seule transaction.

## Qui peut

Action **réservée à l'administrateur** (`source.migrate`) : session récente (le mot de passe est
redemandé si besoin). Une clé d'API, même `admin`, un jeton d'appareil, un membre et un enfant sont
refusés, et la réponse ne dit rien des éditions qui existent.

## Purge amont et restauration

Une copie gardée chez l'ancienne source ne compte plus comme copie indépendante d'un numéro que seule la
nouvelle source liste, tant que le contenu de celle-ci n'a pas été récupéré avec le même hash. Les
tables de migration sont dans l'export complet : une instance restaurée ne retélécharge rien.
