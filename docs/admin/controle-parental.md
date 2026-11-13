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

## Le compte Kavita d'un profil enfant (EF-USR-017, EF-EXP-036)

La barrière d'un enfant est la **bibliothèque** Kavita : tant qu'un compte Kavita y a accès, Kavita
la lui sert, quoi qu'en pense Shelfhold. Shelfhold ne peut pas régler les comptes de Kavita (il n'écrit
jamais dans Kavita) ; il peut en revanche **vérifier** que le compte de chaque enfant est réglé comme
il faut, une fois que vous lui avez dit quel compte est celui de quel enfant.

### Dire quel compte est celui de l'enfant

```
shelfhold profile map-kavita lea --kavita-user lea.k    # nomme le compte
shelfhold profile map-kavita lea --clear                # retire la correspondance
```

L'équivalent HTTP est `PUT /api/v1/profiles/{id}/kavita-account`. C'est une **action réservée** : une
session d'administrateur de moins de 15 minutes, ou la CLI de l'hôte ; jamais une clé d'API (portée
`admin` comprise), un jeton d'appareil, un membre ni un enfant. On y saisit un **nom**, jamais un jeton
ni un mot de passe : Shelfhold ne se connecte pas à Kavita en tant qu'enfant. Kavita ne distingue pas
« Lea » de « lea » ; un même compte ne peut être associé qu'à un seul profil. Seul un profil enfant peut
être associé. Chaque changement est journalisé (le profil, `none`/`set`, jamais le nom).

### Ce que l'audit vérifie

Pour chaque profil enfant associé, la lecture seule de Kavita (`GET /api/Users` et la liste des
bibliothèques) est comparée à l'âge maximal de l'enfant. Le classement d'une bibliothèque est le plus
haut âge maximal des étagères qui l'alimentent ; une bibliothèque qu'aucune étagère n'alimente, ou
qu'alimente une étagère 18+, n'est **pas** une bibliothèque famille.

| Constat | Sens |
|---|---|
| `account_not_found` | aucun compte Kavita ne porte ce nom |
| `account_is_admin` | le compte est administrateur de Kavita : il atteint tout |
| `non_family_library` | le compte atteint une bibliothèque qui n'est pas famille |
| `library_above_max_age` | une bibliothèque famille atteinte monte plus haut que l'âge de l'enfant |
| `no_family_library` | le compte n'atteint **aucune** bibliothèque famille |
| `unrestricted` | le compte n'a aucune restriction d'âge |
| `restriction_above_max_age` | la restriction du compte est au-dessus de l'âge de l'enfant |
| `includes_unknowns` | « inclure les inconnus » est actif |

Un enfant sans compte associé est `unmapped` : Shelfhold ne devine jamais un compte par son nom.
L'audit se rejoue chaque jour (tâche `target.access_audit`) et à la demande :
`GET /api/v1/audit/kavita-accounts` donne le dernier résultat, `POST` le relance. Un constat nouveau
ou modifié est annoncé **une fois** par `kavita.account_audit` (notifiable par défaut) ; le message ne
nomme aucun compte. Une lecture qui échoue laisse l'état `unverified` : un enfant n'est jamais déclaré
conforme sur une lecture qui n'a pas eu lieu. **Rien n'est modifié dans Kavita ni suspendu** : corrigez
le compte dans Kavita.

### Checklist OPDS avec un compte restreint

Le flux OPDS de Kavita ne passe pas par Shelfhold : il applique les réglages du compte qui s'y
connecte. Avant de donner l'adresse à une liseuse d'enfant :

1. Créez dans Kavita un compte **propre à l'enfant**, sans rôle d'administrateur.
2. Limitez-le aux **seules bibliothèques famille** dont l'âge est au plus celui de l'enfant.
3. Réglez une **restriction d'âge** au plus égale à l'âge de l'enfant, et laissez « inclure les inconnus »
   **désactivé**.
4. Associez ce compte au profil (`shelfhold profile map-kavita`), puis lancez l'audit : il doit répondre
   `ok`.
5. Ouvrez le flux OPDS de ce compte (clé d'API du compte, jamais celle de l'administrateur) : seules les
   bibliothèques famille apparaissent ; une série non classée n'apparaît pas.
6. Rejouez l'audit après chaque modification des bibliothèques ou des comptes dans Kavita.
