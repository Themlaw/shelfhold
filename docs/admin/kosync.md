# Synchroniser la progression d'une liseuse (kosync)

Shelfhold parle le protocole de synchronisation de KOReader (kosync) : la liseuse envoie la page où
elle en est, Shelfhold la range dans la progression du profil du jeton, et la rend à une autre
liseuse (ou à la même après une pause). Si la synchronisation de la progression est active pour la
destination Kavita et que vous avez relié **votre propre** clé Kavita, il la relaie aussi vers Kavita,
et reprend ce que Kavita sait (voir « Relais vers Kavita » plus bas).

## Réglage de KOReader

Menu **Synchronisation de la progression** : serveur personnalisé = l'adresse HTTPS de Shelfhold suivie
de `/kosync`, identifiant = l'**identifiant** du jeton d'appareil, mot de passe = son **secret** saisi tel
quel (voir [jetons-appareil.md](jetons-appareil.md)). Méthode de calcul de l'empreinte : **binaire**
(la méthode « nom de fichier » n'est pas prise en charge). Le bouton « Créer un compte » répond toujours
402, avec le message « Les comptes se créent dans Shelfhold » (dans la langue demandée) : les comptes sont les profils de Shelfhold.

## Ce que Shelfhold reconnaît

KOReader nomme un fichier par l'empreinte de ses octets. Shelfhold la retrouve parmi les fichiers
exportés, et parmi les **blocs de série** que le profil a téléchargés (OPDS, « Série complète »).
Une empreinte inconnue, d'un autre profil ou au-dessus du plafond du profil reçoit la même réponse
(`200` à l'envoi, comme si la position était prise, `{}` à la lecture) : rien n'est enregistré, et rien
ne dit si le fichier existe. KOReader traite tout autre statut comme un échec et garde la position dans
sa file d'envoi, en s'arrêtant au premier échec : un seul fichier inconnu bloquerait les suivants. Le
vrai motif du refus reste dans le journal et dans un compteur par jeton. Un
fichier pris ailleurs (un autre serveur OPDS) n'est donc pas synchronisé tant que KOReader pointe vers
Shelfhold.

- **Bloc de série** : la page du bloc est traduite en chapitre et page du chapitre ; la position est
  rangée sur le chapitre. Si le chapitre a été **retiré** depuis le téléchargement du bloc, la position
  n'est pas enregistrée. S'il a été **remplacé**, sa page n'a plus de sens : Shelfhold garde la part du
  chapitre atteinte (une proportion, jamais une page), la compte dans la progression, ne l'envoie jamais
  à Kavita et ne la rend pas à la liseuse. Retéléchargez le bloc.
- **EPUB** : la position (un xpointer) est conservée **telle que la liseuse l'a écrite** (jamais exécutée,
  jamais réécrite en page) et rendue telle quelle. Shelfhold en lit le document du *spine* (`DocFragment[n]`,
  borné : 2 048 caractères, 64 niveaux, 64 caractères par nom ; ce qui sort de la forme connue reste une
  position, sans document). Le livre n'est marqué **« lu » que si** la liseuse arrive à la fin du **dernier
  document du spine** (au moins 99 % du livre) **par une lecture plausible de cet appareil** : un saut à la
  fin, une position au milieu, un spine que Shelfhold ne sait pas lire ou la position d'un autre appareil ne
  marquent rien. Le marquage s'annule comme les autres.
- Un bloc lu d'un trait marque « lu » les chapitres franchis, même si la liseuse n'envoie qu'à la mise
  en veille : la traversée est calculée dans le repère du bloc, à la même vitesse plausible, **par
  appareil** (deux liseuses ne s'enchaînent jamais). Une page que Shelfhold vient de rendre à la
  liseuse et qu'elle renvoie ensuite est un point de départ, pas une lecture.
- **KOReader n'envoie rien de lui-même par défaut** : `auto_sync` est éteint, il n'y a donc ni demande
  à l'ouverture, ni envoi à la fermeture. Activez la synchronisation automatique du module
  (*Progress sync* → *Automatically keep documents in sync*), ou poussez à la main. Sans cela,
  Shelfhold ne voit une position que quand vous la poussez.
- Une page lue ne marque un chapitre « lu » que si la lecture l'a traversé à une vitesse plausible
  (`docs/reference/reading-progress.md`) ; l'heure d'une position est toujours celle du serveur.

## Ce que la liseuse reçoit

La position **la plus avancée** du profil, avec la date de cet enregistrement (jamais « maintenant »).
Relire un chapitre plus ancien ne recule donc pas la position rendue. Une liseuse dont l'horloge est en
avance sur celle du serveur (Kobo sans NTP) ignore les positions distantes.

## Relais vers Kavita

Ce que la liseuse envoie est rangé sur le fichier du chapitre ; la tâche de synchronisation (toutes les
10 minutes, `docs/reference/progress-sync.md`) le transmet à Kavita avec la clé du profil, et reprend
la page que Kavita tient pour ce compte. La page revient à la liseuse dans le repère du bloc.

- **Un chapitre n'est relayé que si Shelfhold et Kavita le comptent pareil.** Shelfhold compare le
  nombre de pages que Kavita annonce pour le chapitre à celui de sa table de pages. Égal : le chapitre
  passe `verified`. Différent (Kavita ignore une image, en compte une de plus), ou noms de pages que
  Kavita pourrait trier autrement : rien n'est envoyé pour ce chapitre, et la page de Kavita n'est pas
  rendue dans le repère du bloc. Les autres chapitres du bloc sont relayés. Santé le signale
  (`relay.page_count_mismatch`, `relay.page_map_unverified`) ; corrigez le chapitre côté Kavita
  (nouveau scan), ou téléchargez un bloc plus récent.
- **Aucune boucle.** Ce que Shelfhold envoie à Kavita n'est jamais relu comme une lecture ; une page
  n'est envoyée que si elle dépasse celle de Kavita ; la dernière page d'un chapitre n'est envoyée
  comme « lu » que si la lecture a traversé le chapitre, sinon Kavita reçoit l'avant-dernière.
- **Un bloc lu d'un trait** : chaque chapitre franchi est marqué « lu » dans Kavita, même s'il n'a
  jamais reçu de position, **si le déplacement part d'un contact récent du même appareil** : au plus
  `SHELFHOLD_READING_RELAY_MAX_SESSION_HOURS` heures (24 par défaut) entre la position précédente et
  celle qui franchit les chapitres. Au-delà (page 1 lundi, chapitre 3 jeudi), le chapitre est marqué
  « lu » chez vous (annulable) mais Kavita n'en est pas informée.
- **Le plafond d'un enfant est relu avant chaque chapitre** : s'il est abaissé pendant une passe,
  plus rien n'est envoyé ni demandé pour les chapitres qui suivent.
- **Un enfant** : rien au-dessus de son plafond n'est lu, ni écrit, ni demandé à Kavita.
- **Compte Kavita partagé** par deux profils : aucun des deux n'est synchronisé.
- **Un EPUB part vers Kavita par son pourcentage seul** (M5 L09). Kavita compte un EPUB en documents,
  Shelfhold en parts du livre : une page de l'un n'est pas une page de l'autre, donc aucune page ni aucun
  index de spine n'est envoyé. Le pourcentage de la liseuse part par la route kosync de Kavita, avec la
  clé du profil, avec le xpointer tel que la liseuse l'a écrit (Kavita le convertit), **jamais en arrière**
  (la part de Kavita est relue juste avant l'envoi). Il faut que le fichier ait une empreinte connue
  (la même que celle de KOReader) ; sans elle, rien n'est envoyé et rien n'est deviné. La position du
  lecteur du navigateur (un `cfi`) n'est pas un xpointer : elle n'est jamais relayée. La part que Kavita
  tient pour un EPUB n'est pas relue dans Shelfhold.
- **La clé du profil voyage dans le chemin de ces deux routes** : c'est le contrat de Kavita (`/api/Koreader/
  {apiKey}/…`), le seul appel où une clé est dans une URL. Le journal de Shelfhold masque la clé (par valeur
  et par forme), mais **Kavita et votre proxy inverse devant lui voient ce chemin** dans leurs propres
  journaux : masquez-le ou ne le conservez pas.

## Santé : les fichiers inconnus

Santé (administrateur seulement) liste, par jeton d'appareil (son identifiant public) et par motif, les
positions que Shelfhold n'a pas prises : `kosync.unknown` (empreinte inconnue, cachée ou d'un autre
profil : on ne les distingue pas), `kosync.integrity` (le fichier ouvert n'est pas celui du bloc),
`kosync.chapter_gone`, `kosync.beyond_block`, `kosync.ambiguous`... L'empreinte n'y figure pas. Les
compteurs sont en mémoire : ils repartent à zéro au redémarrage. Une liseuse qui envoie un fichier pris
sur un autre serveur, ou une ancienne version de bloc purgée, apparaît ici.

## Réglage conseillé : le retour en arrière

La liseuse compare la date de la position reçue au dernier tour de page qu'elle a vu. Shelfhold rend la
position la plus avancée, avec sa propre date, ancienne si vous relisez. Gardez « Synchroniser vers un
état plus ancien » (*Sync to an older state*) sur **Ne pas synchroniser** ou **Demander** : sur
*Silencieux*, la liseuse qui relit un livre serait renvoyée à la fin à chaque réveil.

## Limites

Un EPUB lu dans le navigateur de Shelfhold et sur une liseuse fusionne par la règle du lecteur : la
dernière position (la date de la liseuse est celle du serveur) est celle où vous en êtes ; si l'autre
appareil est plus loin de plus de 2 % du livre, Shelfhold vous le propose une fois. La position de la
liseuse reste sur son fichier, telle qu'écrite.

Une rafale d'envois est fusionnée : au plus une ligne par appareil et par fichier et par minute, qui
remplace la précédente ; la première et la dernière de chaque minute restent.

Les échecs d'authentification sont comptés et limités comme pour l'OPDS (429 avec `Retry-After`) ; un jeton
accepté a un budget de 600 requêtes par minute. Un corps de plus de 16 Kio est refusé (413).
