# Rapatrier avant de démonter un volume de Suwayomi

Cas : des fichiers adoptés ne sont pas sous `/library` mais dans le volume de téléchargement de
Suwayomi, que Kavita voit par un montage supplémentaire. Démonter ce volume ferait disparaître ces
fichiers pour Kavita. **Rapatrier** les copie, vérifiés par empreinte, au **même chemin relatif**
sous `/library`, avant tout démontage (EF-EXP-051). La commande ne supprime rien et ne déplace rien.

Si le dossier de téléchargement de Suwayomi est déjà sous `/library`, ne rapatriez pas : changez
ce dossier dans Suwayomi.

## Prérequis

- Le volume de Suwayomi est déclaré en **lecture seule** dans la correspondance de chemins de la
  destination, en mode `adopt_ro` ([montage-adopt-ro.md](montage-adopt-ro.md)).
- Une racine inscriptible existe sous `/library` pour recevoir les copies, avec la place libre
  (la copie est refusée sans 5 % de marge au-dessus de la taille totale).
- L'instance n'est pas en `restored_behind`.

## Procédure

1. Aperçu obligatoire :
   ```sh
   shelfhold adopt --repatriate --dry-run --destination <id>
   ```
   Chaque fichier adopté y reçoit un état : `copy` (à copier), `present` (déjà là, bon hachage :
   seule la comptabilité manque), `missing` (la source a disparu), `changed` (la source n'a plus
   l'empreinte de l'adoption), `conflict` (un autre fichier occupe la place : jamais écrasé). Le
   nombre de fichiers, la taille et l'espace requis sont indiqués, avec une empreinte.
2. Si un fichier est `missing`, `changed` ou `conflict`, l'application est refusée en bloc :
   corrigez, refaites l'aperçu.
3. Application : `shelfhold adopt --repatriate --confirm <empreinte> --destination
   <id>` (ou `--source <racine>` s'il y a plusieurs volumes en lecture seule). La commande copie, puis
   contrôle chaque fichier copié ; elle ne se termine bien que si le contrôle réussit. Chaque copie passe par le journal d'intentions : un arrêt en cours de route est repris au
   démarrage (copie absente : annulée ; copie au bon hachage : terminée). Relancer ne recopie rien.
4. **Contrôle avant de démonter** : comparez le nombre de séries et de chapitres, côté Shelfhold et
   côté Kavita (lancez un scan de la bibliothèque), à ceux de l'aperçu. La progression de lecture
   de Kavita est liée au chemin, qui n'a pas changé : vérifiez-la sur quelques titres.
5. Retirez le montage, redéployez Kavita, relancez un scan, refaites le contrôle.

La progression de lecture suivie par Shelfhold reste valable (elle est ancrée sur le contenu, pas
sur le disque). Le retour arrière du rapatriement n'existe pas : les copies sont des fichiers
ordinaires de votre bibliothèque, et la source n'a pas été touchée.
