# Contrôle parental : étagères famille, âge dans Kavita, livres EPUB

Le côté pratique (comptes Kavita, appareil partagé) est dans [famille.md](famille.md), le classement des
œuvres dans [a-classer.md](a-classer.md). Cette page donne les règles.

## Les règles d'une étagère famille

Une étagère dont le plafond est **sous 18+** est une étagère famille (`shelfhold shelf create
<nom> --max-age 13+ --target <destination>`).

- **Cloisonnée.** Elle n'est liée qu'à une destination qui sépare les comptes par bibliothèque
  (Kavita) et à une bibliothèque qui ne regarde que son dossier. Une destination sans cloisonnement est
  refusée avec la raison.
- **Vérifiée avant et après.** Avant de la lier, à chaque connexion et chaque jour, Shelfhold lit les
  comptes Kavita qui ont accès à la bibliothèque. Un compte non administrateur dont la restriction
  d'âge est au-dessus du plafond, ou dont « inclure les inconnus » est actif, la refuse ou la met en
  pause (`child_exposure`, sortie `shelfhold target recheck <destination>`). Aucune écriture n'a lieu
  pendant la pause.
- **Kavita 0.9.1 au moins.** La version est lue par le travail d'export, jamais supposée. Une
  version inconnue, hors de la plage prise en charge (la 0.9.0.3 en est) ou connue pour une faille de
  contrôle d'accès refuse toute étagère sous 18+ ; une telle faille met aussi en pause **toutes** les
  étagères de l'instance dès qu'un compte restreint existe.
- **Un fichier n'entre que classé.** Une Œuvre non classée, une Édition nouvelle depuis le dernier
  classement ou dont une suggestion d'âge dépasse le classement sont retenues. L'âge d'une Édition fixé
  explicitement l'emporte sur celui de l'Œuvre pour l'admission ; sinon c'est l'âge effectif de l'Œuvre,
  le plus restrictif de ses Éditions.
- **Retrait.** Un classement relevé au-dessus du plafond ne retire rien seul : voir
  [famille.md](famille.md) (`shelfhold shelf withdraw`, état `withdrawal_pending`).

## L'âge dans Kavita

- **CBZ.** L'âge effectif du catalogue est écrit dans `AgeRating` du ComicInfo à l'export, puis, pour
  chaque série Kavita, poussé par l'API et **verrouillé** (seuls `ageRating` et son verrou changent ;
  aucun fichier n'est réécrit). Un verrou posé par quelqu'un d'autre n'est jamais écrasé : l'écart est
  signalé (`reconcile.drift`) et repoussé au passage suivant.
- **EPUB.** Aucun âge dans le fichier : voir plus bas.
- **Ce que voit le compte restreint** dépend de sa restriction d'âge et de « inclure les inconnus »
  (actif par défaut dans Kavita) : une série sans âge est « inconnue », donc **visible** pour un compte
  qui les inclut. Désactivez-le pour les comptes enfants.
- **Après un `target reconnect`** (Kavita réinstallé), les âges sont repoussés à la toute fin, une fois
  la pause levée et l'audit refait.

## Livres EPUB dans Kavita

La barrière, c'est la **bibliothèque** Kavita : une étagère famille (plafond sous 18+) écrit dans
une bibliothèque qui lui est réservée, et seuls les comptes qui y ont accès la voient. L'âge d'une
série ne sert qu'à trier, à l'intérieur de cette bibliothèque, ce que voit un compte enfant
restreint.

### Aucun âge dans le fichier

Un EPUB exporté ne porte **aucun âge** : ni `dc:subject` d'âge, ni métadonnée propre à Shelfhold.
Kavita ne lit pas d'âge dans un EPUB, et tout `dc:subject` y devient un genre. L'âge vit dans la
base de Shelfhold et passe par l'**API Kavita**, sur la série, **verrouillé**. Le verrou est inscrit
au registre `field_owner` (voir [proprietaire-des-champs.md](proprietaire-des-champs.md)).

Sans cet âge, Kavita classe la série « inconnue ». Ce que voit alors un compte restreint dépend
de son réglage « inclure les inconnus » (`includeUnknowns`, activé par défaut dans Kavita) : sans
lui, la série est cachée ; avec lui, elle est **visible**, quel que soit son âge.

### L'ordre de l'export

Kavita ne crée une série qu'à partir d'un fichier. Pour une série de livres dont l'Œuvre (ou
l'Édition) est classée, l'export suit donc cet ordre :

1. Avant tout plan, Shelfhold interroge Kavita. Il faut qu'il réponde, en version **0.9.1 ou
   plus** (plancher ENF-COMP-001). Sinon, aucun livre n'est exporté.
2. Shelfhold lit les comptes de Kavita. Si un compte non admin atteint la bibliothèque avec
   « inclure les inconnus » et un palier inférieur à l'âge du livre, le premier livre **ne part
   pas** (`export.awaiting_series_age`) et l'alerte `target.child_exposure` nomme le réglage :
   désactivez « inclure les inconnus » pour ce compte.
3. Sinon, le **premier** livre de la série part seul. Kavita le range sous un âge « inconnu »,
   qu'aucun compte restreint au-dessous de son âge ne voit.
4. Après le scan, l'âge de la série est poussé, verrouillé, puis **relu**, sur chaque série
   Kavita où sont ses fichiers. La série n'accepte d'autres livres qu'une fois l'âge relu
   verrouillé, à la valeur voulue ; il est relu encore juste avant chaque écriture. Une série que
   Kavita a coupée en deux (`series_split` dans `shelfhold status`) ne reçoit plus rien.
5. Si Kavita ne répond pas, refuse la poussée ou perd le verrou, les autres livres attendent
   (`export.awaiting_series_age`). Rien n'est perdu : l'export reprend au passage suivant.

Un aperçu ne contacte pas Kavita : il affiche donc toujours `export.awaiting_series_age` pour
ces livres. Seul le job d'export de l'instance en marche lit Kavita et lève l'attente.

Une Œuvre que personne n'a classée n'a pas d'âge à pousser. Une étagère famille la refuse de
toute façon ; une étagère adulte l'exporte sans attendre.

### Reclasser

Classer ou reclasser une Œuvre (`POST /api/v1/works/{id}/rating`, écran « À classer ») exige une session administrateur confirmée il y a moins de 900 s, dans tous les profils ; l'écran redemande le mot de passe sur place puis rejoue le choix. Le classement en lot suit la même règle ; la ligne de commande locale (`shelfhold rate`) n'est pas concernée, et une clé d'API est toujours refusée ([durcir.md](durcir.md)).

Chaque reclassement repousse l'âge, verrouillé, **au début** du passage d'export suivant, avant
toute écriture. Une série reclassée vers le haut ne reçoit aucun nouveau livre tant que le nouvel
âge n'a pas été relu verrouillé. Tant que Kavita affiche un âge inférieur au classement (poussée
refusée), `shelfhold status` le signale (`series_age_stale`) : les livres déjà exportés restent
visibles à l'ancien âge jusqu'à la poussée suivante. Si le reclassement dépasse le plafond de l'étagère, ce n'est plus
une poussée d'âge : c'est le plan de retrait (`shelfhold shelf withdraw`, état d'attente
`withdrawal_pending`), qui sort la série de la bibliothèque des enfants.

### Omnibus et autres reconditionnements

Une Édition `omnibus` (ou `kanzenban`, `deluxe`) de la même Œuvre est une **série Kavita
distincte**, par exemple « Le Livre (fr, omnibus) », avec son propre dossier et **son propre
âge** : celui qu'un admin a fixé pour cette Édition, sinon celui de l'Œuvre.

### Bibliothèque de type Livres

Shelfhold ne modifie jamais une bibliothèque Kavita existante ; `shelf create` n'en crée une que si aucune ne couvre le dossier de l'étagère. Une étagère d'Œuvres `novel`,
`light_novel` ou `ebook` doit viser une bibliothèque Kavita de type **Book**, déclarée par l'admin.
Sinon, le plan affiche l'avertissement `target.library_type_mismatch`. Ne mélangez pas EPUB et CBZ
dans une même étagère.
