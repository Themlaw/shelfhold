# Feuille de route

Source : [`docs/spec/03-roadmap-tdd.md`](docs/spec/03-roadmap-tdd.md) (jalons détaillés, tests écrits d'abord, matrice de traçabilité). Statut actuel : **V1 close (M0 à M3 livrés)**, `v1.0.0-rc.1` puis `v1.0.0` après le test d'utilisabilité du porteur ([guide](docs/governance/test-utilisabilite.md)). Aucune version 0.3 à 0.5 n'a été publiée : la 1.0.0 regroupe M1b, M2 et M3.

Le développement est mené par des agents (voir [AI_POLICY.md](AI_POLICY.md)) : les estimations en heures-homme de la spécification ne s'appliquent pas telles quelles. L'effort est converti en « effort agent » et **suivi dans `docs/governance/velocite.md`**.

**Pas de promotion publique avant 1.0.** Règle de coupe : un jalon qui dépasse 150 % de sa boîte d'effort voit ses éléments « coupables » reportés. Les portes de décision P0–P3 sont dans `docs/governance/portes.md`.

| Jalon | Thème | Résultat | Version |
|---|---|---|---|
| M0 | Fondations | Dépôt public, CI proportionnée verte, image amd64+arm64 sur GHCR, squelette (volumes `/data` et `/store`), `docs/dependencies.md`, spikes S1 (file de jobs), S2 (front), S4 (RAM/image), S5 (contrat Suwayomi) tranchés, gouvernance, posture légale et CRA documentées ; porte P0 | `v0.1.0` |
| M1a | Pont minimal Suwayomi vers Kavita | Une bibliothèque Kavita existante est adoptée sans réécriture et reliée au suivi Suwayomi ; un chapitre suivi arrive normalisé dans Kavita ; authentification et clés d'API ; sans interface, configurable par fichier ; bascule avec retour arrière | `v0.2.0` |
| M1b | Pont complet | Contrôle parental par étagère, « À classer », copie conservée et « Mettre à l'abri », Suwayomi piloté avec sonde de santé, sauvegarde intégrée (second registre d'images reporté en M4) | `v1.0.0` (pas de 0.3) |
| M2 | Sources de livres légales | OPDS générique, Gutenberg (libre de droits aux États-Unis) via catalogues et miroirs, Wikisource, Standard Ebooks (libre de droits aux États-Unis), dossier d'import, fournisseur de métadonnées OpenLibrary | `v1.0.0` (pas de 0.4) |
| M3 | Interface d'atelier, puis 1.0 | Assistant de premier lancement, recherche unifiée, étagères, activité, mode famille (précédé de la porte P2, repli « M3-lite »), `SECURITY.md` complet, parcours d'auteur de plugin | `v1.0.0` |
| M4 | Multi-destinations et synchronisation | Dossier natif, Komga, autres destinations, synchronisation de progression, webhooks, autres fournisseurs de métadonnées ; second registre d'images, miroir du dépôt et vérificateur de mises à jour (ENF-GOV-004) ; écrans reportés de M3 (suppression à trois niveaux, « Même œuvre ? », indicateurs de la page Santé), thèmes B, C et D | `v1.x` |
| M5 | Lecteur intégré | Lecteur EPUB et manga (secondaire), OPDS et kosync propres, OIDC, page légère pour liseuses, PWA | `v1.x` |
| M6 | Écosystème | SDK publié sur PyPI (`shelfhold-sdk`, MIT), plugins tiers isolés, index signé, trackers ; relecture juridique de l'exception de plugin avant publication | `v2.0.0` |

Hors planning (« plus tard ») : moteur YAML, PDF, annotations, Calibre, envoi vers liseuses, autres serveurs de lecture, import V2.

## Critère de sortie commun

À chaque jalon : revue de chevauchement avec Kavita, Komga et Suwayomi (ce qu'ils livrent sort du périmètre), publication de la vélocité, relecture du registre de pré-mortem (`docs/governance/pre-mortem.md`), relecture de `docs/legal/cra.md`.

## Bilan de la V1 (M3 clos)

- **V1 close.** Les 104 exigences de M3 sont couvertes (`tools/traceability.py --milestone M3`) ; M2 et M3 bloquent la CI comme M0, M1a et M1b.
- **Coupes.** Aucun des six éléments coupables de M3 n'a été coupé (ADR-0035, amendement M3). Reportés en M4 : ENF-GOV-004 (second registre, accord écrit du porteur du 2026-11-14, `v1.0.0` signée sur GHCR seul), le client WebSocket de l'événement d'arrivée de Kavita (EF-EXP-034 : polling seul), l'écran de suppression à trois niveaux et l'écran « Même œuvre ? » (à confirmer par le porteur), les indicateurs de valeur de la page Santé, le sélecteur de thèmes. Détail dans `docs/decisions/M3.md`.
- **Avant l'annonce.** Test d'utilisabilité (D3) et vérifications sur appareils par le porteur, avis du juriste (D4, porte P3) : aucune promotion publique avant.
