---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-03 (passe 4) ; 2026-11-03 (passe 5) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 11) ; 2026-11-03 (passe 12) ; 2026-11-04 (arbitrage porteur 1)
decision-makers: mainteneur
---
# ADR-0018 — Authentification, autorisation et scope de visibilité

> **Amendée le 2026-11-02 (passe 1).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Shelfhold est exposé derrière un reverse proxy (par exemple un proxy générique comme Caddy ou Traefik ; aucun proxy n'est supposé depuis l'amendement porteur 1), utilisé par un foyer (dont des enfants) et consommé par des clients non navigateur (OPDS, KOReader, CLI, webhooks, plus tard Mihon et Kobo). Kavita et Suwayomi ont dû greffer le multi-utilisateur et OIDC après coup (Suwayomi #298 restée longtemps ouverte ; comptes utilisateurs fusionnés en 2026 par la PR #623, voir l'amendement passe 5). Les demandes OIDC figurent parmi les plus votées (CWA #90 : 73 👍, ABS #998 : 54 👍, Kavita #2518 : 47 👍).

## Facteurs de décision
- Intégration au reverse proxy (forward-auth) sans usurpation d'en-tête.
- Clients non interactifs (clés, appareils).
- Contrôle parental homogène sur toutes les surfaces.
- Simplicité pour le premier démarrage.

## Options considérées
### A. Comptes locaux argon2id + OIDC (Authlib) + forward-auth (en-tête accepté seulement depuis les CIDR proxy) + clés API scopées hachées + appareils + **un scope de visibilité central**
- Avantages : couvre homelab et clients ; un seul point d'application des restrictions (UI, OPDS, API, kosync, Kobo, Mihon).
- Inconvénients : plusieurs mécanismes à tester ; une erreur de configuration des CIDR permet l'usurpation.
### B. Déléguer entièrement au proxy (forward-auth seul)
- Avantages : rien à implémenter.
- Inconvénients : inutilisable pour OPDS et kosync ; pas de rôles fins ; casse hors proxy.
### C. OIDC seul
- Avantages : standard.
- Inconvénients : impose un fournisseur d'identité au premier démarrage ; clients OPDS incompatibles.
### D. LDAP
- Avantages : annuaires existants.
- Inconvénients : couvert par OIDC et forward-auth pour un homelab (07-explore) ; rejeté.

## Décision
Option A (D6, D32, D34).
- **Jalons** : comptes locaux, forward-auth et clés API en M2 ; OIDC, rôles, scope de visibilité et appareils en M4. Le modèle multi-utilisateur existe dès M1 (toutes les tables d'état ont un `user_id`).
- **Rôles** : admin, membre, restreint/enfant.
- **Permissions** : télécharger, administrer, contenu adulte, synchro Kobo.
- **`VisibilityScope`** : bibliothèques/groupes, `age_rating` max, `is_adult` ; appliqué dans la couche de requêtes, jamais par endpoint.
- **Jetons** par appareil, révocables.
- **Contrôles complémentaires** : limitation des tentatives de connexion, cookies `Secure`/`HttpOnly`/`SameSite`, CSRF.

## Conséquences
- Positives : contrôle parental fiable ; compatible avec tout reverse proxy (Caddy, Traefik, Pangolin…), OPDS et KOReader.
- Négatives : matrice de tests importante (surfaces × rôles) ; doc à fournir sur la configuration des CIDR proxy.

## Risque principal
Contournement du scope de visibilité par une nouvelle route, ou usurpation d'en-tête si les CIDR sont mal configurés. Mitigation : test paramétré généré depuis l'OpenAPI (ENF-SEC-010) ; valeur par défaut des CIDR vide, donc forward-auth désactivé.

## Réversibilité
Haute pour les mécanismes (ajout ou retrait d'adaptateurs) ; faible pour le modèle `user_id` partout, qui est voulu dès le départ.

## Amendement passe 1 (2026-11-02)
- **V1** (A15) : **admin local** + profils famille (M3), **clés API scopées** (clé admin bootstrap créée par CLI dès M1), **forward-auth avec secret partagé obligatoire** (`X-Shelfhold-Proxy-Secret`, comparaison en temps constant) **et** CIDR de confiance (/32 conseillé, réseau dédié `shelfhold_ingress`), accepté seulement sur les préfixes UI/API (jamais `/opds`, `/kosync`, `/kobo`) ; un `Remote-User` sur une route non éligible est rejeté et audité (EF-USR-002, SEC-01).
- **OIDC en M5** (EF-USR-004), avec les appareils. **Kavita reste l'authentification de lecture** en V1 : la famille lit dans Kavita/Kover avec ses comptes Kavita ; correspondance de comptes Shelfhold ↔ Kavita en M4 (EF-USR-012).
- **Contrôle parental** (A31) : portée par **bibliothèque** (≠ étagère), âge max, adulte ; **contenu non classé invisible** pour les profils enfant sur toutes les surfaces Shelfhold (UI, API, recherche distante, vignettes, notifications, SSE ; OPDS/kosync en M5). Dans Kavita, la protection repose sur l'`AgeRating` exporté (EF-EXP-011).
- **Demandes** (EF-USR-010) : politique par profil et par source *Ajouter directement / Demander / Interdit*.
- **Accès de secours** (A38) : `shelfhold admin reset-password` et sessions révocables (EF-USR-011).
- **Jetons en URL** pour OPDS/kosync seulement (M5), hachés, lecture seule, rotation ; limitation de débit des routes publiques par identifiant et IP réelle (XFF du proxy de confiance seulement) (ENF-SEC-008, ENF-SEC-013, A28).
- La permission « synchro Kobo » est réservée à M6 (EF-INT-012).

## Amendement passe 4 (2026-11-03)
Socle d'authentification en M1a (D4, GAP-4-01, GAP-4-02) : compte admin local créé par la CLI (argon2id), session à cookie `HttpOnly`/`Secure`/`SameSite=Strict`, jeton CSRF sur toute requête modifiante, limitation des tentatives, CSP par hash sur toute page servie, page de connexion neutre (EF-USR-021). L'écran « À classer » (M1b) s'appuie sur ce socle ; avant M3, le rôle « classer » est réservé à l'admin. **Clés d'API** : stockées hachées (préfixe affichable, secret montré une seule fois), expiration facultative, `last_used_at`, `shelfhold admin keys list|revoke|rotate`, audit ; une clé révoquée est refusée (401) dès la requête suivante (EF-USR-003).

## Amendement passe 5 (2026-11-03)
**Fait corrigé (E12, T1).** Le contexte disait « Suwayomi #298 toujours ouverte » : ce n'est plus exact. La PR Suwayomi-Server #623 « User Accounts » a été fusionnée le 2026-09-30 (après la stable v2.4.2366) et arrive avec la prochaine stable : mode `server.authMode = "ui_login"`, bibliothèque et suivi **par compte**, téléchargements partagés à compteur de références, JWT d'accès de 5 min et jeton de rafraîchissement ; un nouveau compte peut par défaut installer des extensions. Conséquences pour Shelfhold (EF-SRC-031) :
- **M1a** : la sonde lit le mode d'authentification de Suwayomi ; `ui_login` est refusé avec un message clair (`source.auth_mode_unsupported`), jamais un suivi partiel silencieux. Spike S5 joué en `basic_auth` **et** en `ui_login`.
- **M1b** : AuthFlow `SuwayomiJwt` (connexion, rafraîchissement, rotation) sur un **compte de service dédié** `shelfhold` aux permissions minimales explicites (téléchargement, gestion de sa propre bibliothèque ; jamais l'installation d'extensions) ; la bibliothèque qui fait foi est celle du compte de service ; les suivis des autres comptes ne sont jamais lus en V1.
- **Purge** : retirer la revendication de Shelfhold et mesurer l'espace réellement libéré.
- Côté Kavita (GAP-5-23) : `shelfhold target add kavita` exige `--accept-admin-risk` en M1a.

## Amendement passe 10 (2026-11-03)
K5 (Y1). Le modèle de session et la ré-authentification sont décidés par **ADR-0049** ; résumé :
- Table `session` : `auth_time`, `last_seen_at`, `absolute_expiry`, `remember` dès M1a ; pour l'admin, inactivité 30 min, durée absolue 12 h, « se souvenir de moi » refusé sans confirmation explicite.
- M1b : colonne `fresh_auth_s` de la table de vérité des rôles ; erreur 401 de type `…/problems/reauth-required` définie dans l'OpenAPI v1 ; audit `session_id` + `auth_time`.
- Jetons d'appareil : `subject_user_id` (profil lecteur) distinct de `issued_by` (admin) ; crochet en M3, réalisé en M5 ; la permission et les filtres sont ceux du profil lecteur.
- Pas de codes de récupération web : kit de secours imprimable et second admin facultatif (EF-USR-023).

## Amendement passe 11 (2026-11-03)
L2, L6 (GAP-11-03, GAP-11-06, GAP-11-14, Z1).
- **Clés d'API** : portée `admin` à `expires_at` **obligatoire** (≤ 90 jours par défaut, plafond configurable) ; aucune clé n'exécute une action réservée (EF-USR-024, ADR-0049) ; `subject_user_id` et `issued_by` (M3).
- **Proxy et tunnels** : aucun en-tête d'identité (`Remote-User`, `Tailscale-User-*`…) n'est accepté sans le secret partagé `X-Shelfhold-Proxy-Secret` (règle assouplie par l'amendement porteur 1 : hors forward-auth configuré et hors CIDR de confiance ; `test_tunnel_identity_header_without_forward_auth_rejected`) ; la doc rappelle qu'un tunnel public rend l'instance publique sans forward-auth.
- **Sondes** : `/healthz` et `/readyz` ne sont pas publiées par le proxy inverse (doc et compose de référence) ; `/readyz` révèle l'état des montages et du disque.
- **Forward-auth et OIDC** : actions réservées derrière une session ouverte par mot de passe local ou la CLI ; OIDC avec `prompt=login`, `max_age` et `auth_time` (M5).

## Exigences liées
EF-USR-001, EF-USR-002, EF-USR-003, EF-USR-004, EF-USR-005, EF-USR-006, EF-USR-010, EF-USR-011, EF-USR-012, ENF-SEC-008, ENF-SEC-010, ENF-SEC-013, ENF-OBS-003, EF-USR-021, ENF-SEC-004, EF-SRC-031, EF-INT-024, EF-USR-022, EF-USR-023, EF-USR-024.

## Sources
- https://github.com/Kareadita/Kavita
- https://komga.org/docs/guides/kobo
- https://github.com/Suwayomi/Suwayomi-Server/issues/159
- https://github.com/crocodilestick/Calibre-Web-Automated/issues/319
- https://github.com/koreader/koreader-sync-server
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-01, GAP-4-02)
- `deliverables/review/ARBITRAGES-passe4.md`
- https://github.com/Suwayomi/Suwayomi-Server/pull/623
- https://github.com/Suwayomi/Suwayomi-Server/blob/master/docs/Multi-User-Authentication.md
- `deliverables/review/chasseur-trous-passe5.md`
- `deliverables/review/explorateur-passe5.md`
- `deliverables/review/ARBITRAGES-passe5.md`
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement passe 12 (2026-11-03)
M3, M5 (GAP-12-03, GAP-12-05).

- La « CLI locale » de l'axe crédential (ADR-0049) est le socket Unix `/data/shelfhold.sock` contrôlé par `SO_PEERCRED` ; aucune adresse réseau, `127.0.0.1` compris, n'est un crédential. L'en-tête de forward-auth et le loopback ne changent rien à la règle.
- Dès M1a, une session admin ne réalise une action réservée que si `auth_time` ≤ 300 s (sinon `reauth-required`).

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement arbitrage porteur 1 (2026-11-04)
P1, P4, P5, P7, P8 (`review/ARBITRAGES-porteur1.md`, qui priment).
- **Contexte corrigé** : aucun reverse proxy n'est supposé. Shelfhold doit être sûr derrière n'importe quel proxy (Caddy, Traefik, Authelia, Authentik, Pangolin…) comme sans proxy, sur un réseau domestique. C'est un petit logiciel auto-hébergé, pas une production sensible : aucune faille, mais pas de friction imposée (P1).
- **Forward-auth** (P5) : désactivé par défaut ; une fois activé, CIDR de confiance obligatoire, secret partagé `X-Shelfhold-Proxy-Secret` recommandé et facultatif (il remplace « secret obligatoire » de l'amendement passe 1). Un en-tête d'identité (`Remote-User`, `Tailscale-User-*`…) est ignoré tant que le forward-auth n'est pas configuré et refusé hors CIDR de confiance.
- **Clés d'API** (P4) : expiration facultative pour toutes les portées, `admin` comprise (remplace l'amendement passe 11) ; le refus des actions réservées aux clés et aux jetons est maintenu.
- **Passkeys** (P7) : M1b, décision dans ADR-0050.
- **Bibliothèque d'authentification** (P8), options comparées :
  - *better-auth* : bibliothèque TypeScript complète (sessions, passkeys, OIDC, 2FA). Écartée : elle exige un serveur Node, alors que le front est une SPA statique servie par FastAPI ; le monolithe deviendrait bilingue côté serveur, avec deux sources de vérité pour les sessions et la table de vérité des rôles (ENF-SEC-010).
  - *fastapi-users* : intégré à FastAPI. Écartée : projet passé en mode maintenance, sans WebAuthn.
  - **Retenu** : composition de briques éprouvées et ciblées — `argon2-cffi` (mots de passe), sessions serveur en table SQLite (révocation immédiate, `auth_time`), `webauthn` (py_webauthn, Duo Labs) pour les passkeys, `Authlib` pour OIDC (M5). Le code propre à Shelfhold se limite à la session et à la table de vérité, qui de toute façon lui sont propres.
- Sources : https://www.better-auth.com/docs/introduction · https://github.com/fastapi-users/fastapi-users · https://github.com/duo-labs/py_webauthn · https://docs.authlib.org/ · https://github.com/hynek/argon2-cffi
