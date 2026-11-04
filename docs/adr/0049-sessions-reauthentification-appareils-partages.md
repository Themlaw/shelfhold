---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passe 11) ; 2026-11-03 (passe 12) ; 2026-11-04 (arbitrage porteur 1)
decision-makers: mainteneur
---
# ADR-0049 — Sessions, ré-authentification et appareils partagés

## Contexte et problème
La barrière parentale (ADR-0027, objectif O5) repose entièrement sur l'identité de l'appelant : classer revient à approuver, et seul un admin classe. L'explorateur de la passe 10 (Y1) constate qu'aucun livrable ne traite la durée de vie des sessions, la fraîcheur d'authentification ni l'appareil partagé. Scénario dès M1b : l'admin ouvre « À classer » sur la tablette du salon et laisse la session ouverte ; un enfant classe « Tous publics » une Œuvre 18+, qui part vers la bibliothèque Kavita famille. Aucune faille logicielle, et l'audit attribue le geste à l'admin. En M3 et après : démasquer une demande privée, créer une clé `admin`. En M5 : un jeton OPDS ou kosync créé par l'admin pour **son** compte et collé dans la liseuse familiale donne à l'enfant tout le catalogue.

Reporter crée deux dettes sans retour : des classements dont l'auteur réel est indiscernable (aucune migration ne les réattribue) et des jetons d'appareil sans profil lecteur, qu'il faudrait réémettre en cassant les liseuses déjà appairées.

## Facteurs de décision
- Une session admin oubliée ne doit pas suffire à franchir la barrière parentale.
- L'audit doit dire quelle session et quelle authentification ont produit un geste sensible.
- Les jetons d'appareil doivent porter le profil qui **lit**, pas celui qui **configure**.
- Coût faible tant que le socle d'authentification (M1a) n'est pas écrit.
- Aucun moyen de récupération qu'un enfant pourrait trouver.

## Options considérées
### A. Statu quo : session longue, révocable par la CLI
- Avantages : rien à faire ; confort de l'admin.
- Inconvénients : le scénario ci-dessus reste ouvert ; audit indiscernable ; jetons d'appareil au nom de l'admin.
### B. Sessions courtes pour tous, sans fraîcheur par action
- Avantages : simple.
- Inconvénients : gêne tous les profils ; une session admin de 30 min suffit encore à un enfant qui passe derrière.
### C. Sessions bornées par rôle + fraîcheur par action (« mode sudo ») + jetons au profil lecteur (retenue)
- Avantages : modèle éprouvé (OWASP, NIST SP 800-63B, mode sudo de GitHub) ; seuls les gestes sensibles redemandent le mot de passe ; audit par session ; jetons au bon profil.
- Inconvénients : une colonne de plus dans la table de vérité ; un type d'erreur que les clients doivent gérer ; un parcours d'appairage plus explicite.
### D. Codes de récupération et second facteur web
- Avantages : récupération sans shell.
- Inconvénients : un code imprimé trouvé par un enfant contourne la barrière ; hors des besoins d'un foyer en V1.

## Décision
Option C (arbitrage K5).
- **M1a** : `session.auth_time`, `last_seen_at`, `absolute_expiry`, `remember` ; admin : 30 min d'inactivité, 12 h absolues, « se souvenir de moi » refusé sans confirmation explicite ; identifiant renouvelé à la connexion et à la ré-authentification ; valeurs dans la référence de configuration.
- **M1b** : colonne `fresh_auth_s` de la table de vérité des rôles (ENF-SEC-010) ; actions : classer ou reclasser, changer le maximum d'une étagère ou lier une étagère famille, démasquer, créer, faire tourner ou révoquer une clé ou un jeton, gérer un secret par l'UI, gérer les profils (M3) ; défaut 300 s ; sinon 401 `application/problem+json` de type `…/problems/reauth-required` (RFC 9457), **défini dans l'OpenAPI v1**, avec `auth_age_s` et `max_age_s` ; l'UI redemande le mot de passe sans perdre l'action. L'audit enregistre `session_id` et `auth_time`.
- **Jetons d'appareil** : `device_token(subject_user_id, issued_by, …)`, colonnes posées en M3, jetons émis en M5 ; l'appairage demande d'abord pour quel profil, sans défaut ; filtres et table de vérité évalués sur `subject_user_id`. Invariant I44.
- **Récupération** : pas de codes web ; kit de secours imprimable et second admin facultatif (EF-USR-023) ; la reprise passe par la CLI de l'hôte.
- **Doc famille** : une session adulte de Kavita sur un appareil partagé annule l'accès par bibliothèque ; un compte Kavita par lecteur.

## Conséquences
- Positives : une session oubliée ne suffit plus à classer, démasquer ou créer une clé ; l'audit distingue les sessions ; les liseuses familiales ne voient que le catalogue du profil.
- Négatives : l'admin retape son mot de passe avant un lot de classements (une fois par 5 min) ; les clients doivent gérer `reauth-required`.

## Risque principal
Un enfant qui connaît le mot de passe de l'admin, ou un appareil où le navigateur l'a enregistré. Mitigation : documentation (ne pas enregistrer le mot de passe admin sur un appareil partagé), audit par session, sessions courtes ; c'est hors de la menace traitée par le logiciel.

## Réversibilité
Haute : durées et `fresh_auth_s` sont des réglages ; le type d'erreur et les colonnes restent même si les valeurs changent. Retirer `subject_user_id` après M5 obligerait à réémettre les jetons : c'est pourquoi il est posé dès M3.

## Amendement passe 11 (2026-11-03)
L2 (GAP-11-03, GAP-11-06, GAP-11-14) ; EF-USR-024.
- **Axe crédential** dans la table de vérité (ENF-SEC-010) **dès M1a** : session fraîche (`fresh_auth_s`), session, clé d'API par portée (`read`, `request`, `admin`), jeton d'appareil, CLI locale de l'hôte. Une `fresh_auth_s` ne protège que les sessions : une clé `admin` n'a ni `auth_time` ni confirmation humaine, et une « confirmation tapée » ne vaut rien depuis un script.
- **Actions réservées** (irréversibles, ou touchant aux secrets ou au contrôle parental) : `purge --now`, `tombstone lift`/`list`, `shelf.allowed_profiles` et `max_age_rating`, correspondance profil ↔ compte Kavita, `root takeover|reinit`, `store reinit`, `--accept-admin-risk`, classement, clés et jetons, `secrets export`/`rotate`, démasquage. **Refusées à toute clé d'API, portée `admin` comprise, et à tout jeton d'appareil** (403 `credential-not-allowed`, type de problème de l'OpenAPI v1) ; session fraîche ou CLI locale seulement. La liste `fresh_auth_s` s'étend à `purge --now`, `tombstone lift`, `allowed_profiles`, correspondance profil ↔ Kavita, `root`/`store reinit`.
- **Clés `admin`** : expiration obligatoire, ≤ 90 jours par défaut (EF-USR-003).
- **Sujet des clés** (M3) : `api_key.subject_user_id` et `issued_by`, comme les jetons d'appareil ; la table de vérité s'évalue sur le sujet.
- **Fraîcheur sans mot de passe local** : forward-auth (M3) → session ouverte par mot de passe local ou CLI (`reauth-required` porte `reauth_method`) ; OIDC (M5) → `prompt=login`, `max_age`, vérification du claim `auth_time`.
- Option écartée pour l'instant : portée `destructive` séparée, jamais incluse dans `admin`, à durée de vie obligatoire — **réservée** si un besoin réel apparaît.
- Invariant I47 ; Q65, R63.

## Exigences liées
EF-USR-022, EF-USR-023, EF-USR-021, EF-USR-011, EF-USR-008, EF-USR-006, ENF-SEC-010, EF-USR-015, EF-USR-016, EF-USR-024, EF-USR-003, EF-BIB-052, EF-USR-017.

## Sources
- `deliverables/review/explorateur-passe10.md` (Y1, Y3), `deliverables/review/ARBITRAGES-passe10.md` (K5, K7)
- https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- https://pages.nist.gov/800-63-4/sp800-63b.html
- https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/sudo-mode
- https://www.rfc-editor.org/rfc/rfc9457
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement passe 12 (2026-11-03)
M3, M4, M5 (GAP-12-03, GAP-12-04, GAP-12-05).

- **CLI locale (M3)** : canal unique = socket Unix `/data/shelfhold.sock` (`0600`), uid appelant contrôlé par `SO_PEERCRED` ; jamais TCP, loopback compris : un tunnel ou un proxy dans le même espace de noms réseau ne devient jamais « CLI locale ». Serveur arrêté : la CLI agit hors ligne en prenant le `flock` de `/data/.lock` (ADR-0031). Ligne ajoutée à la table de vérité ; test `test_loopback_http_never_counts_as_local_cli`.
- **Actions réservées par propriété (M4)** : toute action qui supprime définitivement (émet un `FsOp unlink`, vide une corbeille), efface `.shelfhold/`, touche aux secrets ou au contrôle parental, ou confirme une suppression issue du rejeu après restauration. La liste énumérée (dont « Vider la corbeille », `detach` complet, « tout retirer » d'une destination, `tombstone accept-lost-keyring`) est vérifiée par `test_every_unlink_emitter_is_reserved_action`. Le scénario de R63 (« supprimer le contenu » puis « Vider la corbeille » par une clé `admin`) est fermé.
- **Fraîcheur minimale en M1a (M5)** : action réservée ⇒ `auth_time` ≤ 300 s, sinon 401 `reauth-required` (type défini dans l'OpenAPI dès M1a) ; le reste de la colonne `fresh_auth_s` reste en M1b. Test `test_reserved_action_requires_fresh_auth_from_m1a`.

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement passe 13 (2026-11-03)
N2, N5 (GAP-13-02, GAP-13-07).

- **Actions réservées (N2)** : la définition par propriété de la passe 12 s'applique aux actions **déclenchées par un crédential** (session, clé, jeton, CLI). Une **liste fermée d'émetteurs système** en est exemptée : purge du staging orphelin, suppression du fichier sonde `.shelfhold-probe-*`, purge de la corbeille au-delà de sa rétention de 30 jours, purge anticipée par la garde disque ; chacun n'agit que sur des chemins créés par l'hôte, ce qu'un test vérifie. Les commandes de sortie d'EF-INT-023 sont classées : réservées (`source set-secret`, `target rotate-key`, `secrets import`, `tombstone accept-lost-keyring`, `rate`, `root takeover|reinit`, `store reinit`, `target recheck` sur `child_exposure`, confirmation de suppression du rejeu) ou non.
- **Espace de noms PID (GAP-13-07)** : le socket de la CLI refuse un pair dont `SO_PEERCRED.pid` vaut 0 (processus hors de l'espace de noms PID du conteneur : hôte, autre conteneur qui monterait `/data`), sauf `SHELFHOLD_CLI_ALLOW_HOST=1`, journalisé ; la doc dit que l'uid de Shelfhold sur l'hôte vaut un admin (R67) ; en M6, aucun sous-processus de plugin ne voit `/data` ni le socket.
- Tests : `test_every_unlink_emitter_is_reserved_action` (mis à jour), `test_system_unlink_emitters_closed_list_host_paths_only`, `test_reserved_exit_commands_require_fresh_session_or_cli`, `test_local_cli_refuses_peer_outside_container_pid_namespace`, `test_plugin_subprocess_never_sees_data_socket` (M6).

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)

## Amendement arbitrage porteur 1 (2026-11-04)
P1, P2, P3, P7 (`review/ARBITRAGES-porteur1.md`, qui priment). La décision C est conservée sur le principe ; ses valeurs par défaut sont allégées (sécurité proportionnée, P1).
- **Sessions (P2)** : défaut unique pour tous les rôles, 30 jours glissants, « se souvenir de moi » coché. Les durées admin de la décision (30 min, 12 h, « se souvenir » refusé) deviennent le **profil durci** (`docs/admin/durcir.md`). Les colonnes `auth_time`, `last_seen_at`, `absolute_expiry`, `remember` restent.
- **Ré-authentification (P3)** : réduite aux **actions à ré-authentification** — `purge --now`, `tombstone lift`/`list`, `secrets export`/`rotate`, `root`/`store reinit`, vider la corbeille, `detach` complet, confirmation de suppression post-restauration — et au contrôle parental (`max_age_rating`, `allowed_profiles`, correspondance profil ↔ Kavita) **seulement si au moins un profil restreint existe**. Fenêtre par défaut 900 s (remplace 300 s). Classer, démasquer, gérer les clés et les profils ne demandent plus de fraîcheur par défaut ; le profil durci les rajoute. Le scénario du contexte (enfant sur la tablette de l'admin) reste couvert dès qu'un profil enfant existe, puisque le contrôle parental exige alors la fraîcheur ; le classement, lui, redevient accessible à une session admin oubliée hors profil durci, risque accepté par le porteur (R69).
- **Actions réservées** (amendements passes 11–13) : le refus aux clés d'API et aux jetons d'appareil est **maintenu** ; seule la fraîcheur exigée d'une session change.
- **Passkeys (P7)** : la ré-authentification accepte une passkey (ADR-0050) ; toujours aucun code de récupération web (option D inchangée).
- **Risque accepté R69** : hors profil durci, une session admin restée ouverte sur un appareil partagé permet de classer une Œuvre. Mitigation : doc famille (ne pas rester connecté en admin sur un appareil partagé, ou activer le profil durci), audit par session.
