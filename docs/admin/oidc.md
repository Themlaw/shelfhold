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

Sans l'émetteur et l'identifiant, rien n'existe : aucune route ne répond, la page de connexion ne
montre aucun lien.

## Ce qui est vérifié

PKCE (S256), `state` et `nonce`, à usage unique et liés au navigateur qui a commencé. Le jeton
d'identité est accepté seulement si sa signature est celle d'une clé publiée par le fournisseur
(algorithme asymétrique de la liste fermée, jamais `none`), si `iss` est l'émetteur configuré (et
que la découverte du fournisseur le nomme), si `aud` est ce client **et lui seul**, s'il n'est ni
expiré ni émis dans le futur, et si le `nonce` est celui envoyé. Toute autre réponse est un refus
unique : l'écran ne dit pas laquelle des vérifications a échoué (la raison est dans le journal, sans
jamais un `sub` ni un code).

La découverte, les clés et le jeton passent par le Fetcher de Shelfhold et sa garde contre les
requêtes vers le réseau interne : un fournisseur d'un réseau local est celui que vous déclarez dans
`SHELFHOLD_OIDC_ISSUER` ; toute autre adresse annoncée par sa découverte doit être publique, en
https, et une redirection n'est jamais suivie.

## Lier un compte

- **Soi-même** : depuis sa session, **fraîche** (mot de passe ou clé d'accès redonné dans les
  15 dernières minutes), `POST /api/v1/auth/oidc/link` renvoie l'adresse du fournisseur ; la
  personne s'y connecte et revient. Action réservée `oidc.link` : jamais une clé d'API ni un
  jeton d'appareil, jamais un enfant.
- **L'administrateur**, pour un compte existant : `PUT /api/v1/profiles/{id}/oidc` avec le `sub`
  (action réservée `oidc.link_account`, session fraîche d'un administrateur).

Chaque liaison est inscrite à l'audit (le compte, jamais le `sub`).

## Fraîcheur d'une session ouverte par OIDC

Aucun mot de passe local n'a été saisi : la session n'est « fraîche » que dans la mesure où le
fournisseur dit quand la personne s'y est connectée (`auth_time`). Sans cette information, une
action réservée redemande un mot de passe ou une clé d'accès. Le renvoi au fournisseur avec
`prompt=login` et `max_age` arrive avec le lot suivant (L04).
