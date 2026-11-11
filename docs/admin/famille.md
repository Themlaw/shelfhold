# Foyer avec enfants : étagères famille et comptes Kavita

Cette page est celle à lire avant de laisser un enfant se servir de Kavita. Elle dit ce que Shelfhold
garantit, ce qu'il ne peut pas garantir, et ce que vous devez régler vous-même dans Kavita. Le détail
des règles est dans [controle-parental.md](controle-parental.md), le classement des œuvres dans
[a-classer.md](a-classer.md).

## Ce que Shelfhold fait, ce qu'il ne fait pas

La barrière est la **bibliothèque** Kavita : une étagère famille (plafond sous 18+) écrit dans une
bibliothèque réservée, et seuls les comptes qui y ont accès la voient. Shelfhold ne crée aucun compte
Kavita et n'en modifie aucun. Il **lit** les comptes, leur
restriction d'âge et leur option « inclure les inconnus », avant de lier une étagère, à chaque
connexion et une fois par jour, et il met l'étagère en pause (`child_exposure`) si un compte non
administrateur peut voir ce qu'il ne devrait pas.

Il ne peut donc pas empêcher, entre deux vérifications :

- que vous donniez à un compte enfant l'accès à une bibliothèque adulte ;
- qu'un compte enfant n'ait aucune restriction d'âge et lise tout ce qui est visible pour lui ;
- qu'une session adulte reste ouverte sur un appareil que l'enfant utilise.

Ce risque résiduel est celui de l'administrateur de Kavita. La page de Santé et `shelfhold status`
vous disent ce qui a été vu au dernier passage.

## Un compte Kavita par lecteur

Sur tout appareil partagé (tablette du salon, liseuse commune, ordinateur familial), **chaque lecteur
a son propre compte Kavita** :

1. Un compte par enfant, **sans droit d'administration**, avec l'accès à la seule bibliothèque
   famille (celle qui ne regarde que le dossier de l'étagère famille) et une **restriction d'âge**
   au plus égale au plafond de l'étagère.
2. Désactivez **« inclure les inconnus »** pour ces comptes. Sinon, une série sans âge connu leur est
   visible quel que soit son âge. Shelfhold le contrôle, mais seulement pour le compte qu'il voit.
3. Un compte adulte par adulte. Ne partagez pas un compte adulte avec un enfant « pour aller plus
   vite » : la restriction d'âge s'attache au compte, pas à l'appareil.

## Appareil partagé : déconnectez les comptes adultes

Une session Kavita adulte ouverte sur une tablette que l'enfant utilise lui donne tout ce que voit
l'adulte. Shelfhold ne peut ni la voir ni la fermer : l'audit de ses accès ne regarde que les comptes
et les bibliothèques, pas les sessions de Kavita. Ce que vous faites :

- **déconnectez** le compte adulte de Kavita (menu du compte, « Se déconnecter ») quand vous avez fini
  sur un appareil partagé ;
- n'enregistrez pas le mot de passe d'un compte adulte dans le navigateur ou l'application d'un
  appareil partagé ;
- si un appareil a été laissé ouvert, fermez ses sessions depuis Kavita (réglages du compte), puis
  changez le mot de passe du compte si vous avez un doute ;
- côté Shelfhold, le même risque existe pour l'administration : fermez la session de la page
  d'administration, ou activez le profil durci ([durcir.md](durcir.md)), dont la session admin
  s'éteint après 30 minutes d'inactivité.

## Créer l'étagère famille

Elle se crée comme une étagère adulte, avec un plafond sous 18+ :

    shelfhold shelf create "Famille" --max-age 13+ --target <destination> --subpath famille

Le plafond est l'un de `0`, `10+`, `12+`, `13+`, `16+`, `18+` ou `X`. Sous 18+, l'étagère est une
étagère famille : elle n'est acceptée que si la destination la cloisonne (Kavita : par bibliothèque),
si Kavita est en version **0.9.1 ou plus**, et si aucun compte non administrateur qui lit cette
bibliothèque n'est au-dessus de son plafond. Les refus disent la raison et la liste des comptes
concernés, sans rien écrire dans Kavita.

`shelf create` lie l'étagère à la bibliothèque Kavita qui couvre déjà son dossier ; s'il n'y en a
aucune, il en crée une, du nom de l'étagère, qui ne regarde que ce dossier. Pour une étagère famille,
la bibliothèque doit regarder **ce dossier et aucun autre** : si une bibliothèque existante en regarde
d'autres, Shelfhold refuse et le dit. Une bibliothèque famille ne sert qu'**une** étagère famille.
Avant de lier, il relit les comptes qui y ont accès (voir plus haut) ; un refus ne laisse rien derrière
lui et n'écrit rien dans Kavita.

Changer le plafond d'une étagère liée la met en pause (`child_exposure`), dans les deux sens : la sortie
est `shelfhold target recheck <destination>`, qui relit les comptes avec le nouveau plafond. C'est une
action réservée dès qu'un profil restreint existe ([roles.md](roles.md)).

## Ce qui entre dans l'étagère famille

Une Œuvre n'entre dans une étagère famille que si **elle est classée**, et qu'aucune de ses Éditions
n'est plus restrictive que le plafond. Tant qu'elle ne l'est pas, elle est **retenue** : rien n'est
exporté, et elle attend dans « À classer » ([a-classer.md](a-classer.md)). Une Œuvre d'une sous-source
déclarée `safe` est retenue de la même façon (aucune valeur du contrat ne dit « jeunesse » : le sens
sûr est retenu) ; une sous-source `mixed` suggère 18+, une sous-source `nsfw` suggère X, jamais un
classement : seul l'admin classe.

Pour les livres EPUB, l'âge n'est pas dans le fichier : voir
[controle-parental.md](controle-parental.md).

## Retirer ce qui n'a plus sa place

Quand un classement monte au-dessus du plafond, rien ne sort tout seul. `shelfhold shelf withdraw`
montre le plan (les Éditions et leurs fichiers, avec la raison) et sa version ;
`shelfhold shelf withdraw --apply --confirm <version>` l'applique : les fichiers vont à la corbeille
de la racine, l'Édition repasse dans « À classer », et l'audit l'enregistre. Si un export tient le
verrou de l'Œuvre, le retrait échoue sans rien changer et l'état d'attente `withdrawal_pending`
reste affiché : relancez la commande. Il n'y a pas de relance automatique, parce que le retrait est une
action réservée.

Jusqu'au retrait, **le fichier déjà présent reste lisible** par l'enfant. Shelfhold le dit dans
`status` ; c'est à vous de lancer le retrait.

## Adopter une bibliothèque que des enfants lisent déjà

L'adoption n'écrit rien, ne déplace rien et ne cache rien dans Kavita. Les fichiers déjà présents
d'une Œuvre non classée **restent lisibles là où ils sont**, jusqu'à ce que vous les classiez
(`shelfhold rate`) ou les retiriez (`shelf withdraw`). L'aperçu de `shelfhold adopt` le dit, et le suivi
de ces Œuvres est suspendu (`follow_suspended`) : aucun nouveau chapitre n'est demandé avant le
classement.
