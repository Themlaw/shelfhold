# Le montage `adopt_ro` : lire le volume de Suwayomi sans pouvoir l'abîmer

Pour rapatrier des fichiers qui vivent dans le volume de téléchargement de Suwayomi
([rapatrier-avant-demontage.md](rapatrier-avant-demontage.md)), Shelfhold doit les lire. Il ne doit
jamais y écrire : ce volume est monté **en lecture seule** par Docker **et** déclaré `adopt_ro`
dans la correspondance de chemins (EF-EXP-060).

## Mise en place

1. Dans le compose de Shelfhold (le compose d'exemple de ce dépôt en montre la ligne, en commentaire,
   à décommenter), montez le dossier de téléchargement de Suwayomi en `:ro` sous un
   chemin à part, par exemple :
   ```yaml
   volumes:
     - /srv/suwayomi/downloads:/upstream/suwayomi:ro
   ```
2. Déclarez-le dans la correspondance de chemins de la destination, avec le chemin que **Kavita**
   voit pour ce même volume :
   ```sh
   shelfhold config set path_map.kavita '[
     {"local": "/library/manga", "remote": "/manga"},
     {"local": "/upstream/suwayomi", "remote": "/suwayomi", "mode": "adopt_ro"}
   ]'
   ```
   Modifier `path_map.*` exige une session administrateur ([headless.md](headless.md)).

## Ce que le mode garantit

- Aucune écriture, aucun renommage, aucune suppression, aucun bail ni dossier `.shelfhold` n'y
  est posé ; la purge ne le touche jamais ([purge.md](purge.md)).
- Il sert à l'adoption et au rapatriement, rien d'autre.
- Ne le laissez monté que pendant la bascule : un volume en lecture seule oublié est un chemin
  de plus à surveiller.

## Après le rapatriement

Retirez le montage du compose de Shelfhold (et celui de Kavita), retirez l'entrée `adopt_ro` de
`path_map.<cible>`, redéployez, puis contrôlez le nombre de séries et de chapitres dans Kavita.
