# Feuille de route

Source : [`docs/spec/03-roadmap-tdd.md`](docs/spec/03-roadmap-tdd.md) (jalons détaillés, tests écrits d'abord, matrice de traçabilité). Statut actuel : **pré-alpha, M0**.

Le développement est mené par des agents (voir [AI_POLICY.md](AI_POLICY.md)) : les estimations en heures-homme de la spécification ne s'appliquent pas telles quelles. L'effort est converti en « effort agent » et **suivi dans `docs/governance/velocite.md`**.

**Pas de promotion publique avant 1.0.** Règle de coupe : un jalon qui dépasse 150 % de sa boîte d'effort voit ses éléments « coupables » reportés. Les portes de décision P0–P3 sont dans `docs/governance/portes.md`.

| Jalon | Thème | Résultat | Version |
|---|---|---|---|
| M0 | Fondations | Dépôt public, CI proportionnée verte, image amd64+arm64 sur GHCR, squelette (volumes `/data` et `/store`), `docs/dependencies.md`, spikes S1 (file de jobs), S2 (front), S4 (RAM/image), S5 (contrat Suwayomi) tranchés, gouvernance, posture légale et CRA documentées ; porte P0 | `v0.1.0` |
| M1a | Pont minimal Suwayomi vers Kavita | Une bibliothèque Kavita existante est adoptée sans réécriture et reliée au suivi Suwayomi ; un chapitre suivi arrive normalisé dans Kavita ; authentification et clés d'API ; sans interface, configurable par fichier ; bascule avec retour arrière | `v0.2.0` |
| M1b | Pont complet | Contrôle parental par étagère, « À classer », copie conservée et « Mettre à l'abri », Suwayomi piloté avec sonde de santé, sauvegarde intégrée, second registre d'images | `v0.3.0` |
| M2 | Sources de livres légales | OPDS générique, Gutenberg via catalogues et miroirs, Wikisource, Standard Ebooks, dossier d'import, fournisseur de métadonnées OpenLibrary | `v0.4.0` |
| M3 | Interface d'atelier, puis 1.0 | Assistant de premier lancement, recherche unifiée, étagères, activité, mode famille (précédé de la porte P2, repli « M3-lite »), `SECURITY.md` complet, parcours d'auteur de plugin | `v1.0.0` |
| M4 | Multi-destinations et synchronisation | Dossier natif, Komga, autres destinations, synchronisation de progression, webhooks, autres fournisseurs de métadonnées | `v1.x` |
| M5 | Lecteur intégré | Lecteur EPUB et manga (secondaire), OPDS et kosync propres, OIDC, page légère pour liseuses, PWA | `v1.x` |
| M6 | Écosystème | SDK publié sur PyPI (`shelfhold-sdk`, MIT), plugins tiers isolés, index signé, trackers ; relecture juridique de l'exception de plugin avant publication | `v2.0.0` |

Hors planning (« plus tard ») : moteur YAML, PDF, annotations, Calibre, envoi vers liseuses, autres serveurs de lecture, import V2.

## Critère de sortie commun

À chaque jalon : revue de chevauchement avec Kavita, Komga et Suwayomi (ce qu'ils livrent sort du périmètre), publication de la vélocité, relecture du registre de pré-mortem (`docs/governance/pre-mortem.md`), relecture de `docs/legal/cra.md`.
