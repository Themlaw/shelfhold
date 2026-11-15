# Synchroniser la progression d'une liseuse (kosync)

Shelfhold parle le protocole de synchronisation de KOReader (kosync) : la liseuse envoie la page où
elle en est, Shelfhold la range dans la progression du profil du jeton, et la rend à une autre
liseuse (ou à la même après une pause). Il ne relaie rien vers Kavita pour l'instant.

## Réglage de KOReader

Menu **Synchronisation de la progression** : serveur personnalisé = l'adresse HTTPS de Shelfhold suivie
de `/kosync`, identifiant = l'**identifiant** du jeton d'appareil, mot de passe = son **secret** saisi tel
quel (voir [jetons-appareil.md](jetons-appareil.md)). Méthode de calcul de l'empreinte : **binaire**
(la méthode « nom de fichier » n'est pas prise en charge). Le bouton « Créer un compte » répond toujours
402 : les comptes sont les profils de Shelfhold.

## Ce que Shelfhold reconnaît

KOReader nomme un fichier par l'empreinte de ses octets. Shelfhold la retrouve parmi les fichiers
exportés, et parmi les **blocs de série** que le profil a téléchargés (OPDS, « Série complète »).
Une empreinte inconnue, d'un autre profil ou au-dessus du plafond du profil reçoit la même réponse
(`202` à l'envoi, `{}` à la lecture) : rien n'est enregistré, et rien ne dit si le fichier existe. Un
fichier pris ailleurs (un autre serveur OPDS) n'est donc pas synchronisé tant que KOReader pointe vers
Shelfhold.

- **Bloc de série** : la page du bloc est traduite en chapitre et page du chapitre ; la position est
  rangée sur le chapitre. Si le chapitre a été retiré ou remplacé depuis le téléchargement du bloc, la
  position n'est pas enregistrée : retéléchargez le bloc.
- **EPUB** : la position (un xpointer) est conservée telle quelle ; elle ne marque pas le chapitre lu.
- Une page lue ne marque un chapitre « lu » que si la lecture l'a traversé à une vitesse plausible
  (`docs/reference/reading-progress.md`) ; l'heure d'une position est toujours celle du serveur.

## Ce que la liseuse reçoit

La position **la plus avancée** du profil, avec la date de cet enregistrement (jamais « maintenant »).
Relire un chapitre plus ancien ne recule donc pas la position rendue. Une liseuse dont l'horloge est en
avance sur celle du serveur (Kobo sans NTP) ignore les positions distantes.

## Limites

Les échecs d'authentification sont comptés et limités comme pour l'OPDS (429 avec `Retry-After`) ; un jeton
accepté a un budget de 600 requêtes par minute. Un corps de plus de 16 Kio est refusé (413).
