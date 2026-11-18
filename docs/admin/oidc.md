# Se connecter avec un fournisseur d'identité (OpenID Connect)

Shelfhold sait ouvrir une session par un fournisseur OpenID Connect (Authelia, Keycloak, Authentik,
Zitadel…). C'est **facultatif** : le mot de passe local et les clés d'accès restent, et la page de
connexion reste neutre (aucun nom d'outil, aucune image).

## Ce que ça fait, et ce que ça ne fait pas

- La connexion **ne crée jamais de compte**. Le fournisseur dit qui est la personne par son `sub`
  (un identifiant stable) ; Shelfhold cherche le compte **déjà lié** à ce `sub`. Pas de compte lié :
  la connexion est refusée, sans dire si la personne était connue.
- Aucun rapprochement par nom ou par adresse e-mail : seul le `sub`, sous l'émetteur configuré.
- **Un profil enfant n'est jamais lié** (ni par lui, ni par l'administrateur) et n'ouvre jamais de
  session par OIDC, même si une ligne avait été écrite autrement.
- Une personne du fournisseur n'a qu'un compte, et un compte n'a qu'une identité par fournisseur.
- Le forward-auth reste refusé sur `/opds`, `/kosync`, `/kobo` et `/lite` : ces routes gardent leurs
  propres identifiants.

## Régler

Déclarez chez le fournisseur un client « code d'autorisation » dont l'adresse de retour est
`<SHELFHOLD_PUBLIC_URL>/api/v1/auth/oidc/callback`, puis :

| Variable | Rôle |
|---|---|
| `SHELFHOLD_PUBLIC_URL` | obligatoire, en https |
| `SHELFHOLD_OIDC_ISSUER` | adresse https du fournisseur (sans paramètre ni identifiant) |
| `SHELFHOLD_OIDC_CLIENT_ID` | identifiant du client |
| `SHELFHOLD_OIDC_CLIENT_SECRET` | secret du client, facultatif (PKCE suffit à un client public) |
| `SHELFHOLD_OIDC_MAX_AGE_S` | âge maximal, en secondes, de la connexion chez le fournisseur qui rend une session fraîche (300 par défaut ; entre 30 et 900, et jamais plus que `SHELFHOLD_FRESH_AUTH_S`) |

Sans l'émetteur et l'identifiant, rien n'existe : aucune route ne répond, la page de connexion ne
montre aucun lien.

## Ce qui est vérifié

PKCE (S256), `state` et `nonce`, à usage unique et liés au navigateur qui a commencé. Le jeton
d'identité est accepté seulement si sa signature est celle d'une clé publiée par le fournisseur
(algorithme asymétrique de la liste fermée, jamais `none`), si `iss` est l'émetteur configuré (et
que la découverte du fournisseur le nomme), si `aud` est ce client **et lui seul**, s'il n'est ni
expiré ni émis dans le futur (des dates finies, sans quoi c'est un refus), et si le `nonce` est
celui envoyé. Toute autre réponse est un refus
unique : l'écran ne dit pas laquelle des vérifications a échoué (la raison est dans le journal, sans
jamais un `sub` ni un code).

La découverte, les clés et le jeton passent par le Fetcher de Shelfhold et sa garde contre les
requêtes vers le réseau interne : un fournisseur d'un réseau local est celui que vous déclarez dans
`SHELFHOLD_OIDC_ISSUER` ; toute autre adresse annoncée par sa découverte doit être publique, en
https, et une redirection n'est jamais suivie.

## Lier un compte

- **Soi-même** : depuis sa session, **fraîche** (mot de passe ou clé d'accès redonné dans les
  15 dernières minutes), `POST /api/v1/auth/oidc/link` renvoie l'adresse du fournisseur ; la
  personne s'y connecte et revient. Action réservée `oidc.link_own` : jamais une clé d'API ni un
  jeton d'appareil, jamais un enfant.
  Au retour du fournisseur, **rien n'est encore écrit** : une page de Shelfhold dit « lier
  l'identité X au compte Y ? », et la liaison n'est faite qu'après avoir cliqué sur « Lier », dans
  la session qui l'a demandée (fraîche, avec son jeton CSRF). Sur un navigateur partagé, quelqu'un
  d'autre qui se connecte chez le fournisseur ne peut donc pas lier son identité à votre compte.
- **L'administrateur**, pour un compte existant : `PUT /api/v1/profiles/{id}/oidc` avec le `sub`
  (action réservée `oidc.link_account`, session fraîche d'un administrateur). Il peut lier **son
  propre compte** ou celui d'un **membre adulte**, jamais celui d'un autre administrateur (chacun
  lie le sien depuis sa session) ni un profil enfant.

Chaque liaison est inscrite à l'audit (le compte, jamais le `sub`), et le titulaire du compte voit
ses liens (`GET /api/v1/auth/oidc/identities` : le fournisseur, la date, et si un administrateur les
a faits). Le retour d'une liaison, d'une preuve ou d'un refus arrive sur l'écran des réglages
(`/reglages/compte/oidc`), qui montre l'émetteur, les liens du compte et permet de lier ou de délier.

## Délier

`DELETE /api/v1/auth/oidc/identities` (soi-même, action réservée `oidc.unlink_own`) ou
`DELETE /api/v1/profiles/{id}/oidc` (un administrateur, `oidc.unlink_account`, pour son compte ou
celui d'un membre adulte, jamais un autre administrateur). Ce sont des actions réservées, **fraîches
dans tous les profils** : jamais une clé d'API ni un jeton d'appareil. La déliaison ferme les
sessions que le fournisseur avait ouvertes pour ce compte, est inscrite à l'audit
(`oidc.unlinked`) et **le titulaire est prévenu** : liaison faite par un administrateur,
déliaison (par lui ou par un administrateur), par le canal qui prévient déjà les personnes
(le condensé de notification, sans identifiant ni nom de l'auteur ; les heures calmes ne le
retiennent pas). **Il part à la destination de notification du titulaire** (réglages de
notification, un compte Apprise à lui) **et à elle seule** : jamais dans les canaux partagés, que
lit tout le foyer (un enfant, l'administrateur qui l'aurait fait). Sans destination à lui, rien ne
part, et la trace reste sur l'écran `/reglages/compte/oidc` (« dernier lien retiré le… »). Invitez
chaque adulte à régler la sienne. Une liaison au mauvais `sub` se corrige en déliant, puis en liant
de nouveau.

## Après une restauration

Tant que l'avis de restauration est actif (`shelfhold status`), la connexion par le fournisseur est
**suspendue**, comme celle par clé d'accès : un lien retiré depuis la sauvegarde est de nouveau
vivant. Vérifiez les comptes, puis `shelfhold restore acknowledge` : la commande **liste les liens**
du fournisseur tels qu'ils sont dans la base (le compte et l'émetteur, jamais le `sub`) en plus des
clés d'accès modifiées (`shelfhold status` les montre déjà avant l'acquittement, qui est ce qui
rouvre la connexion). Lier un compte reste possible. Le début d'une connexion chez le fournisseur
a son propre plafond (30 par minute et par adresse), distinct de celui des options de clé d'accès.

## Rotation des clés du fournisseur

Un jeton qui ne nomme pas sa clé (`kid`) est **refusé** quand le fournisseur en publie plusieurs
(pendant une rotation, par exemple) : Shelfhold ne devine pas laquelle utiliser. Un fournisseur
qui omet le `kid` ne fonctionne que lorsqu'une seule clé est publiée.

## Fraîcheur d'une session ouverte par OIDC

Aucun mot de passe local n'a été saisi : une session ouverte par le fournisseur n'est **jamais**
« fraîche » d'elle-même, et ce que le fournisseur dit de `auth_time` à la connexion n'est pas cru
(rien ne lui avait demandé de reconnecter la personne). Pour une action réservée qui demande une
preuve récente, le 401 `reauth-required` propose, en premier, la méthode `oidc` (puis le mot de
passe et la clé d'accès, qui restent possibles). `POST /api/v1/auth/oidc/reauth` renvoie alors la
personne au fournisseur avec `prompt=login` et `max_age` (`SHELFHOLD_OIDC_MAX_AGE_S`). Au retour, la
session redevient fraîche **seulement si** :

- le jeton est valide (mêmes vérifications que la connexion) ;
- c'est **la même identité** que celle qui lie le compte (une autre personne du fournisseur est
  refusée) ;
- le claim `auth_time` est **présent** et date de moins de `max_age` secondes (une minute d'avance
  d'horloge est tolérée) : absent, trop ancien ou dans le futur, c'est un refus, et rien n'est cru ;
- la session vit encore et aucun avis de restauration n'est actif.

La session est alors aussi fraîche que la connexion chez le fournisseur, pas davantage, et reçoit un
nouveau jeton. Le retour se fait sur l'écran des réglages d'identité (`?oidc=reauth`) ; l'action
demandée y est reprise. Un fournisseur qui ignore `prompt=login` et répond depuis sa propre session
donne un `auth_time` ancien : il est refusé, et le mot de passe reste la voie de secours. Le
contrat est celui de `max_age` : si la session du fournisseur date de moins de `max_age` secondes,
son `auth_time` est accepté même s'il n'a pas redemandé le mot de passe. Chez un fournisseur
conforme, `prompt=login` le force toujours.

**Limite à connaître.** `prompt=login` protège d'une session restée ouverte chez le fournisseur,
**pas d'un compte du fournisseur compromis** : qui connaît les identifiants de la personne chez lui
ouvre une session, puis rend la sienne fraîche, et peut alors enregistrer une clé d'accès. Un
fournisseur qui impose une double authentification ferme cette porte ; sinon, gardez-la en tête
(l'enregistrement d'une clé d'accès est à l'audit, `passkey.register`).
