# Passer à l'équipe préférée

Une série peut être publiée par plusieurs équipes. Choisir « Préférer » sur la fiche de la série ne fait que ranger les
équipes : **aucun fichier déjà servi ne change**. Les chapitres à venir suivent la préférence, les chapitres déjà
servis restent ce qu'ils sont. Pour remplacer ceux-ci, la fiche propose une seconde étape.

## Les deux temps

1. **« Voir ce qui serait remplacé »** : un aperçu, qui n'écrit rien. Il compte :
   - les chapitres qui **seraient remplacés** (l'équipe préférée en a récupéré le fichier) ;
   - les chapitres **bloqués**, avec la raison ;
   - les chapitres **en attente** (l'équipe préférée ne les a pas encore récupérés : ils restent comme ils sont et suivent
     la préférence à leur arrivée).
2. **« Remplacer maintenant »** : l'application, mise en file comme une tâche d'écriture. Elle prend le bail de la
   bibliothèque, comme le cycle de suivi, puis remplace les fichiers un par un, **au même emplacement** : Kavita garde
   le nom du fichier, donc la place du chapitre et sa progression. L'ancien fichier va à la corbeille de sa racine
   (30 jours) et se restaure. L'écran « Résultat » dit ce qui a été remplacé, bloqué ou mis en attente.

Les deux temps sont une **action réservée** (`team.switch`) : une session d'administrateur récente, jamais une clé d'API
(même `admin`, même en lecture seule), jamais un membre ni un enfant.

## Ce qui n'est jamais remplacé

| Raison affichée | Pourquoi |
|---|---|
| Original irremplaçable | Plus aucune source ne peut redonner le chapitre : son remplacement demande de taper son identifiant, ce qu'un remplacement automatique ne fait pas. |
| Original adopté | Le fichier a été adopté sur place (montage `adopt_ro`) : Shelfhold ne l'écrit jamais. |
| Fait partie d'un tome | Un tome assemblé se réécrit en entier, par sa propre commande. |
| Déjà lu | Un lecteur a fini le chapitre et l'étagère dit de ne pas mettre à niveau les chapitres lus. |
| Dossier gelé, opération en cours | Une racine gelée ou une intention ouverte sur le fichier : rien n'est écrit, pas même l'intention. |
| Révision suspecte, âge non admis, destination fermée, fichier modifié à la main | Les gardes de la montée de version s'appliquent telles quelles. |

Un chapitre bloqué garde son fichier **et** son équipe : l'étiquette « exportée depuis » et le fichier servi ne divergent
jamais.

## Un aperçu périmé n'est pas appliqué

L'aperçu garde l'empreinte de ce qu'il a dit. Si un chapitre arrive, est lu ou change entre l'aperçu et l'application,
l'application ne part pas (`team_switch.stale`) : rien n'est remplacé, demandez un nouvel aperçu. Un aperçu non appliqué
depuis plus d'un jour est supprimé au prochain.

## Si Shelfhold s'arrête en route

Chaque remplacement est une intention du journal : un arrêt brutal à n'importe quelle étape se reprend au démarrage,
avant toute autre tâche (le chemin sert toujours un seul fichier, jamais aucun, jamais deux, et l'ancien n'est jamais
perdu). La tâche reprend ensuite et finit le travail ; ce qui était fait n'est pas refait. Un lecteur qui ouvre le
chapitre pendant le remplacement reçoit l'ancien fichier ou le nouveau, entier.

`jobs retry` et `jobs replay` d'une tâche de bascule demandent la même session récente.
