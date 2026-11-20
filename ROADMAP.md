# Feuille de route

Source : [`docs/spec/03-roadmap-tdd.md`](docs/spec/03-roadmap-tdd.md) (jalons détaillés, tests écrits d'abord, matrice de traçabilité). Statut actuel : **M5 clos dans le dépôt (candidate `v1.2.0-rc.1` publiée ; `v1.2.0` attend les essais sur appareil réel du porteur et le contradicteur de jalon)** ; M4 livré en `v1.1.0` ; V1 close (M0 à M3 livrés), `v1.0.0-rc.1` puis `v1.0.0` ; le test d'utilisabilité ([guide](docs/governance/test-utilisabilite.md)) conditionne l'annonce publique, plus le tag. Aucune version 0.3 à 0.5 n'a été publiée : la 1.0.0 regroupe M1b, M2 et M3.

Le développement est mené par des agents (voir [AI_POLICY.md](AI_POLICY.md)) : les estimations en heures-homme de la spécification ne s'appliquent pas telles quelles. L'effort est converti en « effort agent » et **suivi dans `docs/governance/velocite.md`**.

**Pas de promotion publique avant 1.0.** Règle de coupe : un jalon qui dépasse 150 % de sa boîte d'effort voit ses éléments « coupables » reportés. Les portes de décision P0–P3 sont dans `docs/governance/portes.md`.

| Jalon | Thème | Résultat | Version |
|---|---|---|---|
| M0 | Fondations | Dépôt public, CI proportionnée verte, image amd64+arm64 sur GHCR, squelette (volumes `/data` et `/store`), `docs/dependencies.md`, spikes S1 (file de jobs), S2 (front), S4 (RAM/image), S5 (contrat Suwayomi) tranchés, gouvernance, posture légale et CRA documentées ; porte P0 | `v0.1.0` |
| M1a | Pont minimal Suwayomi vers Kavita | Une bibliothèque Kavita existante est adoptée sans réécriture et reliée au suivi Suwayomi ; un chapitre suivi arrive normalisé dans Kavita ; authentification et clés d'API ; sans interface, configurable par fichier ; bascule avec retour arrière | `v0.2.0` |
| M1b | Pont complet | Contrôle parental par étagère, « À classer », copie conservée et « Mettre à l'abri », Suwayomi piloté avec sonde de santé, sauvegarde intégrée (second registre d'images reporté en M4) | `v1.0.0` (pas de 0.3) |
| M2 | Sources de livres légales | OPDS générique, Gutenberg (libre de droits aux États-Unis) via catalogues et miroirs, Wikisource, Standard Ebooks (libre de droits aux États-Unis), dossier d'import, fournisseur de métadonnées OpenLibrary | `v1.0.0` (pas de 0.4) |
| M3 | Interface d'atelier, puis 1.0 | Assistant de premier lancement, recherche unifiée, étagères, activité, mode famille (précédé de la porte P2, repli « M3-lite »), `SECURITY.md` complet, parcours d'auteur de plugin | `v1.0.0` |
| M4 | Multi-destinations et synchronisation | Dossier natif, Komga, autres destinations, synchronisation de progression, webhooks, autres fournisseurs de métadonnées ; second registre d'images, miroir du dépôt et vérificateur de mises à jour (ENF-GOV-004) ; écrans reportés de M3 (suppression à trois niveaux, « Même œuvre ? » ; les indicateurs de la page Santé passent en « Plus tard »), thèmes B, C et D | `v1.1.0` (candidates `v1.1.0-rc.1` et `-rc.2`) |
| M5 | Lecteur intégré | Lecteur d'images et d'EPUB pour le contenu non exporté et « Lire un extrait » (secondaire à Kavita), OPDS-PSE, OPDS 2.0 et kosync propres, jetons d'appareil enregistrés, OIDC, page légère `/lite` pour liseuses, PWA et copies hors ligne, KEPUB | `v1.2.0` (candidate `v1.2.0-rc.1`) |
| M6 | Écosystème | SDK publié sur PyPI (`shelfhold-sdk`, MIT), plugins tiers isolés, index signé, trackers ; relecture juridique de l'exception de plugin avant publication | `v2.0.0` |

Hors planning (« plus tard ») : indicateurs de valeur de la page Santé, migration d’une liaison d’une racine à l’autre (une copie, pas un renommage), moteur YAML, PDF, annotations, Calibre, envoi vers liseuses, autres serveurs de lecture, import V2.

## Critère de sortie commun

À chaque jalon : revue de chevauchement avec Kavita, Komga et Suwayomi (ce qu'ils livrent sort du périmètre), publication de la vélocité, relecture du registre de pré-mortem (`docs/governance/pre-mortem.md`), relecture de `docs/legal/cra.md`.

## Bilan de la V1 (M3 clos)

- **V1 close.** Les 104 exigences de M3 sont couvertes (`tools/traceability.py --milestone M3`) ; M2 et M3 bloquent la CI comme M0, M1a et M1b.
- **Coupes.** Aucun des six éléments coupables de M3 n'a été coupé (ADR-0035, amendement M3). Reportés en M4 : ENF-GOV-004 (second registre, accord écrit du porteur du 2026-11-14, `v1.0.0` signée sur GHCR seul), le client WebSocket de l'événement d'arrivée de Kavita (EF-EXP-034 : polling seul), l'écran de suppression à trois niveaux et l'écran « Même œuvre ? » (à confirmer par le porteur), les indicateurs de valeur de la page Santé, le sélecteur de thèmes. Détail dans `docs/decisions/M3.md`.
- **Coupables de M4 (D-P2).** Au-delà de 353 h d'effort agent (150 % de la boîte de 235 h), seuls ces trois éléments glissent à M5, sans débat : les thèmes B, C et D avec le sélecteur (~10 h), le plugin ComicVine (~3 h) et le transport WebSocket des événements de Kavita (~4 h, EF-EXP-034). Les thèmes A ne sont jamais coupés ; aucun coupable ne touche à la sécurité, au contrôle parental ni à l'intégrité.
- **Coupables de M5.** Les trois éléments reportés de M4 (thèmes B, C et D avec le sélecteur, plugin ComicVine, transport WebSocket des événements de Kavita, EF-EXP-034) sont les coupables de M5 ; aucun nouveau coupable n'est ajouté. Aucun ne touche à la sécurité, au contrôle parental ni à l'intégrité.
- **Avant l'annonce.** Test d'utilisabilité (D3) et vérifications sur appareils par le porteur, avis du juriste (D4, porte P3) : aucune promotion publique avant.

## Bilan de M4 (clôture, `docs/decisions/M4.md`)

- **Livré.** 47 exigences couvertes (`tools/traceability.py --milestone M4`, bloquant dans la CI depuis la clôture) : dossier natif, Komga, CWA et Kavita, plusieurs destinations par étagère, synchronisation de la progression (Kavita, kosync, Suwayomi), webhooks signés, import `.tachibk`, métadonnées enrichies, thèmes B, C et D. ≈ 80 h d'horloge sur une boîte de 235 h.
- **Coupes.** Aucune : les trois coupables de M4 (thèmes B, C et D, plugin ComicVine, transport du hub de Kavita) sont livrés.
- **Reporté en M5** (écrit dans `docs/decisions/M4.md`, section de L43) : l'écran « Même œuvre ? » (EF-BIB-029), l'audit Komga et le relais de scan appelés par la passe (une liaison familiale vers Komga reste refusée), la garde « chapitre lu » de la montée de version, `plan_migration` des normaliseurs. Les indicateurs de valeur de la page Santé n'ont eu aucune carte en M4 : « Plus tard » (décision du porteur).
- **Encore ouvert à la clôture.** L47 (alias d'export, #348), la lecture réelle dans Kover (D-P5, `[[partial]]` d'EF-EXP-026 inscrit ; repli : conformité OPDS-PSE et KOReader en CI), le second registre et le miroir en direct (ENF-GOV-004, D-P1 : `v1.1.0` signée sur GHCR seul sans jeton), le contradicteur de jalon.

## Bilan de M5 (clôture, `docs/decisions/M5.md`)

- **Livré.** 25 exigences couvertes (`tools/traceability.py --milestone M5`, **bloquant** dans la CI depuis la clôture), 30 cartes, 31 PR de lot : route `/content` et neutraliseur v2, manifeste de publication et positions, lecteur d'images et d'EPUB (foliate-js derrière `ReaderEngine`), « Continuer la lecture », PWA et copies hors ligne, `/lite` sans JavaScript, OPDS-PSE et 2.0, KEPUB, sélections enregistrées, registre d'appareils et rotation, OIDC, passage d'EPUB par kosync. ≈ 49 h d'horloge sur une boîte de 210 h.
- **Coupes.** Aucune : les trois coupables de M5 (OPDS 2.0, KEPUB, sélections enregistrées) sont livrés.
- **Reporté en M6** (écrit dans `docs/decisions/M5.md`, « Reporté en M6 ») : relais de scan et audit de Komga (~4 h), annulation d'une marque « lu » posée par une traversée d'appareil, étagère de l'enfant remplie à l'arrivée d'un livre demandé, appelant ou suppression de l'enveloppe de balayage des renditions, rotation du secret et écran des webhooks, « Lire hors ligne » pour un profil non administrateur, écran « Même œuvre ? » (EF-BIB-029). La synchronisation Kobo (EF-INT-012, Q21) reste **conditionnelle** et non planifiée : à relire dans les notes de version de Kavita à l'entrée de M6.
- **Encore ouvert à la clôture.** Les essais sur appareil réel (KOReader, `/lite` depuis une Kobo, EPUB sur téléphone ; D-P3) et l'« ok » du porteur avant `v1.2.0` ; le contradicteur de jalon ; le second registre et le miroir (ENF-GOV-004, `[[deferred]]`, `v1.2.0` sur GHCR seul sans jeton) ; la revue de chevauchement, faite sans réseau (lecture des notes de version de Kavita, Komga et Suwayomi à compléter).
