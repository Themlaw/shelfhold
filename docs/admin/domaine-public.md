# Domaine public : le pays de l'instance et l'avertissement

Gutenberg et Standard Ebooks publient des œuvres **libres de droits aux États-Unis**. Ce n'est
pas la même chose qu'être libre de droits partout : en France, par exemple, la durée est la vie
de l'auteur plus 70 ans, plus les prorogations de guerre, plus 30 ans pour un auteur mort pour la
France, et une traduction a ses propres droits (EF-SRC-024, ADR-0032). Shelfhold ne qualifie pas
l'usage que vous faites d'un livre, et ce qui suit n'est pas un avis juridique.

## Le pays de l'instance

`instance.jurisdiction` est un code de pays à deux lettres. Il est **déduit de la langue du
système** à l'installation et reste « non confirmé » tant que vous ne l'avez pas posé :

```
shelfhold config set instance.jurisdiction FR
```

`shelfhold status` le rappelle tant que ce n'est pas fait. Le calcul utilise la valeur non
confirmée en attendant. Quand la langue du système ne nomme aucun pays, la valeur de départ est
`FR`.

## Ce que le calcul regarde

- **Toutes les contributions** qui ont un droit propre : auteur, illustrateur, traducteur,
  adaptateur, préfacier. La date de décès vient de la fiche du contributeur ; la date la plus
  tardive l'emporte. Les colonnes `author_death_year`, `translator` et `translator_death_year` de
  l'Édition sont un cache dérivé de ces contributions.
- **La base des droits** que la source déclare (`rights_basis`) : `us_public_domain`,
  `worldwide_public_domain`, `licence` ou `user_supplied`. Un livre sous licence (CC BY-SA, par
  exemple) ne donne **aucun** avertissement.
- Pour les États-Unis, l'année qui compte est celle de la première publication de l'Œuvre.

## Ce que l'avertissement n'est pas

Il est **indicatif** et **jamais bloquant** : aucune règle n'empêche un ajout. Il prend la forme
d'un événement et d'une ligne de `shelfhold status`. Quand une date de décès manque, Shelfhold dit
« aucune information sur les droits dans votre pays » : jamais « libre de droits ».
