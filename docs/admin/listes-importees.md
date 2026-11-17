# Importer une liste de lecture

Une liste de livres voulus, écrite en texte simple, devient en un geste une **étagère de lecture** (ce que la bibliothèque a déjà)
et des **demandes** (le reste), que l'administrateur lit et décide sur **un seul écran**. Rien n'est cherché dans une source :
l'import ne regarde que le catalogue.

## Le texte

Un titre par ligne, avec un ISBN si vous en avez un :

```text
# à lire cet été
Le Jardin des brumes
Atlas des marées ; 978-0-306-40615-7
Un autre titre (édition annotée)
```

- `#` ouvre une ligne de commentaire, les lignes vides sont ignorées ;
- l'ISBN peut suivre le titre après `;`, une tabulation ou entre parenthèses, ou occuper la ligne seul ;
- **bornes** : 64 Kio, 100 lignes, 300 caractères par ligne. Une liste au-delà est refusée **en entier** (jamais coupée), avec le
  mot qui dit pourquoi (`too_large`, `too_many_lines`, `line_too_long`, `not_text`, `empty`) ;
- le texte est lu comme non fiable : un chemin, une adresse ou une balise n'est qu'un titre, qui n'est jamais suivi.

## Depuis l'écran

`/a-approuver/liste` : choisissez pour qui (vous, ou un enfant si vous êtes administrateur), collez le texte ou choisissez un
fichier, puis « Importer ». Le résultat dit combien de lignes sont déjà dans la bibliothèque (elles vont sur l'étagère de
lecture) et combien sont demandées, avec l'adresse de l'écran de validation. Chaque demande s'y décide comme dans « À
approuver » : un âge pour une demande d'enfant, une raison pour un refus.

## Depuis la ligne de commande

```bash
shelfhold request --from-list ete.txt                    # la liste de l'administrateur
shelfhold request --from-list ete.txt --for-profile noa  # une liste pour un enfant
cat ete.txt | shelfhold request --from-list - --name "Été"
```

## Pour un enfant

Faire une liste à un enfant est une décision parentale (action réservée `reading_shelf.attach` : session d'administrateur
fraîche, jamais une clé). Trois choses ne changent pas :

- son **étagère de lecture** ne porte que ce qu'elle voit déjà : un livre au-dessus de son plafond, ou rangé sur une étagère qui
  ne lui est pas ouverte, n'y est pas, et elle n'apprend pas qu'il existe ;
- chaque demande faite en son nom **demande un âge à l'approbation**, comme toute demande d'enfant ;
- **rien n'est cherché** dans une source pour elle. Une ligne que le catalogue n'a pas reste une demande en attente : l'admin la
  met de côté (« à acheter », « à emprunter ») ou la refuse.

Un enfant ne fait pas de liste elle-même : elle demande parmi les résultats de sa recherche.

## Édition annotée

Une ligne qui réclame une édition précise (« annotée », « édition critique », « commentée », « avec notes ») porte un
avertissement : Shelfhold ne peut pas promettre cette édition. L'import n'est pas refusé ; vérifiez l'édition avant d'approuver.

## Vie privée

La liste d'un membre est aussi privée que ses demandes : l'administrateur voit qu'elle existe et ses totaux, pas ses titres. Le lot
d'une autre personne est, pour un membre ou un enfant, une absence (404).
