---
status: accepté
date: 2026-11-04
decision-makers: porteur, mainteneur
---
# ADR-0050 — Passkeys (WebAuthn) pour la connexion et la ré-authentification

## Contexte et problème
Le socle d'authentification (ADR-0018, EF-USR-021) repose sur un mot de passe argon2id. L'arbitrage porteur P1 allège les mesures qui ajoutent de la friction (sessions longues par défaut, ré-authentification réduite, ADR-0049) : la ré-authentification restante doit donc coûter le moins possible, et le mot de passe reste le seul rempart d'une instance exposée sans proxy d'authentification. Aucun second facteur n'existe : ADR-0049 a écarté les codes de récupération web, qu'un enfant pourrait trouver. Le porteur demande les passkeys (P7).

## Facteurs de décision
- Résistance à l'hameçonnage et à la réutilisation de mots de passe, sans infrastructure en plus.
- Ré-authentification en un geste (empreinte, visage, code de l'appareil).
- Aucun moyen de récupération qu'un enfant pourrait trouver (ADR-0049, option D).
- Fonctionnement honnête hors HTTPS (réseau domestique en `http://IP`).
- Pas de nouveau runtime serveur (monolithe Python, ADR-0018 amendée P8).

## Options considérées
### A. Mot de passe seul (statu quo)
- Avantages : rien à faire ; fonctionne partout, `http://` compris.
- Inconvénients : hameçonnable, réutilisable ; ré-authentification fastidieuse, donc désactivée en pratique.
### B. TOTP (application d'authentification)
- Avantages : universel, fonctionne hors HTTPS.
- Inconvénients : hameçonnable ; appelle des codes de secours (écartés par ADR-0049) ; friction à chaque connexion ; secret partagé stocké côté serveur.
### C. Passkeys WebAuthn, mot de passe en repli (retenue)
- Avantages : résistantes à l'hameçonnage (liées à l'origine) ; aucun secret côté serveur (clé publique seule) ; synchronisées par les gestionnaires de mots de passe des plateformes ; ré-authentification en un geste ; recommandées par NIST SP 800-63B comme authentificateur résistant à l'hameçonnage.
- Inconvénients : contexte sécurisé exigé (HTTPS ou `localhost`) ; une dépendance de plus ; parcours d'enregistrement à concevoir.
### D. Passkeys obligatoires, mot de passe supprimé
- Avantages : surface minimale.
- Inconvénients : inutilisable en `http://IP` ; perte de l'appareil = reprise par la CLI seulement ; contraire à P1 (pas de friction imposée).

## Décision
Option C (arbitrage porteur P7, P8), jalon **M1b** (EF-USR-025).
- Tout compte peut enregistrer une ou plusieurs passkeys, nommées et révocables ; connexion par identifiant découvrable (`residentKey=required`, `userVerification=required`) ; attestation `none` (aucune liste d'authentificateurs imposée).
- La ré-authentification d'ADR-0049 (amendée P3) accepte une passkey et remet `auth_time` à jour.
- Le mot de passe reste le moyen de repli ; un compte ne peut pas retirer son dernier moyen de connexion.
- Toujours aucun code de récupération web : `shelfhold admin reset-password` sur l'hôte réinitialise le mot de passe et peut révoquer les passkeys.
- Stockage : `webauthn_credential(user_id, credential_id, public_key, sign_count, transports, aaguid, name, created_at, last_used_at)` ; compteur de signature vérifié quand l'authentificateur en fournit un (0 constant toléré pour les passkeys synchronisées).
- RP ID dérivé de `SHELFHOLD_PUBLIC_URL` ; en `http://` hors `localhost`, l'UI masque l'enregistrement et explique pourquoi.
- Bibliothèque : `webauthn` (py_webauthn, Duo Labs) côté serveur ; API navigateur `navigator.credentials` côté SPA, sans dépendance JS lourde.
- Audit de chaque enregistrement, connexion et révocation.

## Conséquences
- Positives : connexion et ré-authentification sans mot de passe ; l'allègement de P1–P3 ne réduit pas la sécurité réelle pour qui utilise HTTPS ; aucune donnée secrète en base.
- Négatives : environ 12 h en M1b ; doc HTTPS nécessaire ; matrice de tests élargie (authentificateur simulé).

## Risque principal
Changement de domaine ou de `SHELFHOLD_PUBLIC_URL` : les passkeys liées à l'ancien RP ID deviennent inutilisables. Mitigation : le mot de passe reste en repli ; `shelfhold status` signale un RP ID différent de celui des passkeys enregistrées ; doc « changer de domaine ».

## Réversibilité
Haute : les passkeys sont un moyen de connexion additionnel ; les retirer laisse le mot de passe intact. La table est indépendante du reste du modèle.

## Exigences liées
EF-USR-025, EF-USR-021, EF-USR-022, EF-USR-024, ENF-SEC-008.

## Sources
- https://www.w3.org/TR/webauthn-3/
- https://fidoalliance.org/passkeys/
- https://github.com/duo-labs/py_webauthn
- https://pages.nist.gov/800-63-4/sp800-63b.html
- `deliverables/review/ARBITRAGES-porteur1.md` (P7, P8)
