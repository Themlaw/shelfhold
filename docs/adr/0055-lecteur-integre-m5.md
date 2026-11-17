---
status: acceptée
date: 2026-11-17
decision-makers: mainteneur
---
# ADR-0055 — Lecteur intégré de M5 : rôle, chemin du contenu, port `ReaderEngine`

## Contexte et problème
L'ADR-0012 décide d'un lecteur intégré secondaire livré en M5, mais laisse ouverts trois points que M5 ne peut pas coder sans réponse :
- **son rôle exact** (06 Q22, ouverte « début M5 ») : aperçu, concurrent de Kavita, ou lecteur du seul contenu non exporté ;
- **l'isolation du contenu** : 01 (EF-LEC-005, ENF-SEC-004) accepte « une origine **ou** un chemin dédié », ce qui demande un DNS, un certificat et un proxy de plus si l'on choisit l'origine ;
- **la forme du port `ReaderEngine`** et les moteurs qu'il enregistre, alors que 03 range `test_pdfjs_is_eval_supported_false` en M5 pour un format (PDF) que 01 (EF-LEC-004) date « Plus tard ».

S'y ajoutent des contradictions relevées à l'extraction des livrables : 01 (EF-LEC-009) et 03 demandent pour kosync un `409` sur hash ambigu et un repli par `metadata`, alors que l'ADR-0053, acceptée et déjà livrée en M4 (L47, L50), impose `200` à tout `PUT` et « ne devine jamais » ; 02 donne à `x-auth-key` le md5 du mot de passe alors que l'ADR-0053 le fixe au md5 du secret du jeton d'appareil ; 02 annonce « OIDC en V2 » alors que l'ADR-0018 le place en M5 ; 01 date ENF-A11Y-002 de M3 seule et EF-EXP-058 de M1a seule alors que 03 leur donne un volet M5.

## Facteurs de décision
- Kavita reste le lecteur principal de la famille (01 §1, point 4 ; ADR-0020) : Shelfhold ne devient pas un second Kavita.
- La définition de fini de M5 : « lire un extrait dans Shelfhold sans exporter ».
- Contenu non fiable (ADR-0047, ENF-SEC-004) : aucun script de livre ne s'exécute, tout octet sort par `content_response`, le lecteur lit le **dérivé neutralisé**.
- Contrôle parental (ADR-0027, ENF-SEC-010) : chaque route de contenu est revérifiée contre la visibilité du profil à chaque service.
- KOReader arrête sa file d'envoi au premier statut autre que 200 (ADR-0053, amendement du 2026-11-16) : un relais qui répond autre chose casse la synchronisation des liseuses.
- Peu d'infrastructure à la charge du porteur : pas d'origine, de DNS ni de certificat supplémentaires par défaut.

## Décisions du porteur (D-P1, D-P2) et décisions déléguées (D6 à D9)

### D-P1 — Rôle du lecteur (06 Q22, tranchée)
**Option 3.** Le lecteur est **complet pour le contenu non exporté** (copie conservée, ADR-0028) : lecture, réglages, position enregistrée sur l'ancre `content` (ADR-0045, EF-LEC-014). Pour **tout autre contenu**, il propose « **Lire un extrait** » : lecture bornée, sans lecture complète du catalogue. **Kavita reste le lecteur principal.** Un contenu déjà exporté se lit dans Kavita ; Shelfhold n'ouvre pas un second lieu de lecture complet pour lui.

Écartées : l'aperçu seul (peu utile hors atelier) et le lecteur complet de tout le catalogue (concurrence frontale avec Kavita, deux lieux de lecture, conflits de progression).

L'extrait est borné par le serveur (nombre de pages ou de caractères, valeur par défaut réglable) et ne fait pas progresser la lecture d'une Œuvre : il n'écrit pas de position qui compterait dans « Continuer la lecture ».

### D-P2 — Chemin du contenu (EF-LEC-005, ENF-SEC-004)
Le contenu est servi sur **`/content/*`, même origine**, isolé par une **CSP `sandbox` sur chaque réponse**. Plus précisément :
- toute réponse de `/content/*` passe par `content_response` (aucune route ad hoc) : `Content-Security-Policy: sandbox`, `X-Content-Type-Options: nosniff`, `Content-Disposition: attachment` pour SVG et HTML, aucune réponse inline hors images matricielles ;
- le livre s'affiche dans une **iframe `sandbox` sans `allow-same-origin` ni `allow-scripts`** ; ses ressources (feuilles de style, images, polices) sont lues par `blob:` ;
- la CSP de la page du lecteur ajoute `font-src`, `media-src` et `frame-src blob:` (`frontend/scripts/csp.ts`, `infrastructure/http_headers.py`) ; aucune directive ne desserre `default-src` ou `script-src` ;
- la visibilité du profil est revérifiée à **chaque** service : un classement ou un retrait qui change fait cesser le service tout de suite.

**Origine séparée en option, pas par défaut.** Une seconde origine (sous-domaine) reste possible par un réglage `SHELFHOLD_CONTENT_ORIGIN`, documentée avec compose et étapes à la charge du porteur (DNS, TLS, proxy), **jamais exigée**. Fixer ce réglage est une **action réservée** (table de vérité d'ENF-SEC-010, énumération d'EF-USR-024). Le défaut est la même origine ; le porteur dit s'il veut l'origine séparée sur son déploiement.

**Risque connu à prouver en L11 et L12.** Une iframe sans `allow-same-origin` ne laisse pas la page parente lire son document : le moteur EPUB ne peut pas paginer en accédant directement au DOM de l'iframe. La première réalisation prouve, par un test d'intégration, la voie retenue (document neutralisé servi en `blob:` et échange par messages, sans script dans le livre). Si aucune voie ne tient sans `allow-same-origin`, la décision n'est **pas** assouplie en silence : un amendement de cette ADR est exigé avant L12.

### Port `ReaderEngine`
Un seul port côté front (`frontend/src/lib/reader/`) que le lecteur utilise ; deux moteurs :
- **moteur d'images maison** : manga paginé (LTR et RTL, doubles pages) et webtoon vertical (liste d'images, `IntersectionObserver`, préchargement) ; le décodage d'image passe par le worker sans réseau (02 §5.8) ;
- **moteur EPUB sur foliate-js** (reflow et fixed-layout) : foliate-js est **vendu en sous-module git épinglé par commit** (ADR-0012), jamais en paquet npm, et **importé par son seul adaptateur** (règle ESLint `no-restricted-imports`, posée par L00).

Le port expose : ouvrir une publication, rendre une page ou un segment, produire et restaurer un locator, annoncer les raccourcis clavier (ENF-A11Y-002) et libérer ses ressources. Un moteur déclare les types de média qu'il sait lire ; le **registre** choisit le moteur par type de média et refuse la construction si un moteur ne tient pas ses garde-fous. Le lecteur lit le **dérivé neutralisé** d'un EPUB `untrusted` (ADR-0047), jamais l'original d'une source.

### D6 — kosync : l'ADR-0053 amendée prime
01 (EF-LEC-009) et 03 sont alignés sur l'ADR-0053 amendée le 2026-11-16 :
- **`200` à tout `PUT`**, y compris pour une empreinte inconnue, cachée, d'un autre profil, ambiguë ou une garde d'intégrité qui refuse ; le motif réel va au journal et à un compteur par jeton ; seul un corps mal formé reste un `422` (ou un `413`) ;
- **aucune devinette** : le champ `metadata` est reçu et ignoré, aucun appariement par titre, taille ou métadonnée ; un hash tenu par deux supports ne se résout pas ;
- **`GET`** d'un document non résolu : `200 {}` ;
- **aucun `409` en HTTP** ;
- `x-auth-key` est le md5 du **secret du jeton d'appareil** (jamais du mot de passe du compte) ; 02 est corrigé ;
- les deux tests de 03 sont **renommés** : `test_kosync_metadata_fallback_match` devient `test_kosync_metadata_never_used_to_match` et `test_kosync_ambiguous_hash_409` devient `test_kosync_ambiguous_hash_never_guesses`.

### D7 — Aucun moteur PDF en M5
EF-LEC-004 reste « Plus tard ». Le port `ReaderEngine` n'enregistre **aucun moteur PDF** en M5. `test_pdfjs_is_eval_supported_false` (ENF-SEC-004, CVE-2024-4367) prouve deux choses : aucun moteur PDF n'est enregistré ; un moteur PDF qui s'enregistrerait sans `isEvalSupported: false` fait **échouer la construction du registre**. La garde est donc posée avant que le format n'arrive.

### D8 — Seuil de conflit des positions (06 Q47, tranchée)
Le principe de G10 est conservé : `client_ts` le plus récent, borné par `server_ts`, historique conservé (EF-LEC-007). Un **conflit n'est proposé** que si la position perdante est **plus avancée** que la gagnante de plus de **2 %** de la publication, avec **au moins 3 pages**. Une seule proposition par (profil, support) jusqu'au choix de l'utilisateur ; en dessous du seuil, fusion silencieuse avec historique. Valeurs par défaut, réglables.

### D9 — Dates dans 01
- **ENF-A11Y-002** est datée « M3 (atelier) / M5 (lecteur) » : son texte nomme déjà le lecteur de M5.
- **EF-EXP-058** porte son volet M5 : la liaison `pull` (OPDS, kosync Shelfhold) ne sert que depuis un exemplaire lisible (`/store` ou copie d'arbre). Elle est datée « M1a / M5 (volet `pull`) ».
- Les avertissements de `traceability.py --milestone M5` (« listée par 03 pour M5, mais 01 donne… ») restent acceptés pour les autres volets, comme en M2 à M4.

## Conséquences
- **Positives** : le lecteur tient la définition de fini sans concurrencer Kavita ; l'isolation par défaut ne demande rien au porteur ; la sync des liseuses reste sûre ; les contradictions de 01, 02 et 03 sont fermées avant que du code en dépende.
- **Négatives** : deux moteurs à maintenir (images et EPUB) ; foliate-js n'a pas de release stable (R13) ; la même origine repose sur l'exactitude de la CSP `sandbox` par réponse, d'où l'option d'origine séparée ; la contrainte de l'iframe sans `allow-same-origin` est à prouver.
- **Aucune migration** dans ce lot : documentation seule.

## Risque principal
L'isolation du contenu. Mitigation : une seule sortie (`content_response`), tests d'attaque sur le corpus d'EPUB piégés (enfant, membre, clé d'API, IDOR sur la ressource d'autrui), CSP du lecteur testée, option d'origine séparée.

## Réversibilité
Bonne pour le moteur (derrière `ReaderEngine`) et pour l'origine (un réglage). Moyenne pour le rôle du lecteur (D-P1) : élargir au catalogue entier est un ajout de routes, pas une refonte, mais rouvre Q22.

## Amendements et corrections portés par cette ADR
- **ADR-0012** : rôle selon D-P1, chemin `/content/*` par défaut (D-P2), port `ReaderEngine`, aucun moteur PDF en M5 (D7). Le texte d'origine de pdf.js « livré avec le lecteur » est suspendu.
- **ADR-0018** : OIDC et registre d'appareils en M5 ; jetons d'appareil livrés en M4 par l'ADR-0053 ; `x-auth-key` = md5 du secret du jeton.
- **ADR-0053** : l'amendement du 2026-11-16 prime sur 01 et 03 (D6) ; passage d'EPUB repris en M5 (L09).
- **01** : EF-LEC-009 (D6), ENF-A11Y-002 et EF-EXP-058 (D9). **02** : `x-auth-key` (route kosync), index des ADR (« OIDC en V2 » devient « OIDC en M5 »). **03** : tests kosync renommés, D7, D8, D-P2. **04** : module `reading/`. **06** : Q22 et Q47 « tranchées ».

## Exigences liées
EF-LEC-001, EF-LEC-002, EF-LEC-003, EF-LEC-005, EF-LEC-006, EF-LEC-007, EF-LEC-009, EF-LEC-014, EF-EXP-058, ENF-SEC-004, ENF-A11Y-002.

## Sources
- ADR-0012, ADR-0018, ADR-0020, ADR-0027, ADR-0028, ADR-0045, ADR-0047, ADR-0053 (et son amendement du 2026-11-16)
- `docs/spec/06-points-discutables.md` (Q22, Q47)
- https://github.com/johnfactotum/foliate-js
- https://github.com/advisories/GHSA-wgrm-67xf-hhpq (pdf.js, CVE-2024-4367)

## Acceptation (2026-11-17)
Acceptée par le mainteneur pour D-P1 et D-P2 (décisions déléguées à l'exécution de M5, écrites telles quelles) ; D6 à D9 sont des décisions déléguées révisables par écrit.
