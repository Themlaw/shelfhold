# Synchronisation de la progression

Chaque membre du foyer lit dans Kavita avec son compte ; Shelfhold retient où chacun s'est arrêté
et le tient à jour **dans les deux sens**, sans jamais écraser une lecture plus avancée. Le contrat
précis (ancrage sur le fichier lu, anti-écho, plafonnements) est dans
[`docs/reference/progress-sync.md`](../reference/progress-sync.md) ; cette page dit ce que fait
l'instance, quand, et où lire son état.

## Ce qui se synchronise, et avec quelle clé

Le job `progress.sync` tourne **toutes les 10 minutes**. Pour chaque liaison Kavita où la
synchronisation est allumée, il **tire** d'abord l'état que Kavita tient, puis **pousse** en
dernier la progression prouvée de Shelfhold — toujours avec **la clé du membre** (liée depuis
Réglages, Compte Kavita), jamais avec le compte de service. En cas d'écart, la dernière écriture
gagne, à l'heure **du serveur Shelfhold** : une horloge de Kavita décalée ne décide de rien.

Ce que la passe ne fait jamais :

- **elle ne recule pas** : une page n'est envoyée que si elle dépasse celle de Kavita, relue juste
  avant l'écriture ;
- **elle n'invente pas un « lu »** : un regard jeté à la dernière page est poussé comme
  l'avant-dernière page ; « lu » seulement si une traversée a marqué toutes les unités du
  fichier (et côté tirage, une page de Kavita avant la fin n'est jamais enregistrée comme finie) ;
- **elle ne demande rien au-dessus du plafond d'un enfant** : un chapitre qu'un profil enfant ne
  peut pas lire n'est ni interrogé ni écrit ;
- **elle n'échoue jamais le job** : une clé refusée, révoquée ou un Kavita muet termine la passe
  du membre (`unavailable`), les autres attendent le tour suivant.

## Allumer la synchronisation d'une liaison : pas encore disponible

La synchronisation se règle **par liaison** (la colonne `sync_progress` de la liaison, Kavita
seulement). **Aucune interface ne l'écrit encore** : ni écran, ni commande, ni route — le job lit
le réglage, rien ne l'allume. Elle sera une action réservée quand sa surface existera. Jusque-là,
une instance de production n'a pas de synchronisation active.

## La progression survit aux rangements

Quand vous renommez une série, la reclassez ou migrez une liaison (dans une même racine), la
progression de chaque membre est **portée** : la passe tire l'état de tous les membres avant le
premier déplacement, déplace, fait scanner, retrouve la série par son dossier, puis repousse. Un
membre dont la clé échoue **arrête le lot avant le déplacement** (`progress_not_pulled`) : rien
n'est perdu pour un renommage à moitié fait. Un rescan de Kavita qui renumérote une série est
réparé au tour suivant, avec ou sans plan de renommage.

## Où lire l'état

- **Réglages, Compte Kavita** (ou `GET /api/v1/sync-status`) : pour chaque membre et chaque liaison
  Kavita ouverte, la clé est-elle liée (`linked` ou `no_token`), et quand ont eu lieu le dernier
  tirage et la dernière poussée. Un membre lit ses lignes, l'administrateur lit celles de tous.
- **Liseuses KOReader** : le serveur kosync local de Shelfhold reçoit la page du lecteur et la
  relaie vers Kavita — réglage et limites dans [kosync.md](kosync.md), jetons dans
  [jetons-appareil.md](jetons-appareil.md).
- **Suwayomi** : le job `read_state.push` dit à la source quels chapitres ont été lus dans
  Shelfhold, pour qu'une série lue ici ne revienne pas « non lue » là-bas — contrat dans
  [`docs/reference/read-state-push.md`](../reference/read-state-push.md).

## Ce qui n'existe pas encore

- **Allumer la synchronisation d'une liaison** : pas encore disponible (voir plus haut).
- **La synchronisation avec une autre destination que Kavita** : le job ne lit que les liaisons
  Kavita où le réglage est allumé ; rien n'est écrit pour une autre destination.
