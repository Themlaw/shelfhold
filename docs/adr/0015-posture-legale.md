---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1, réécriture complète) ; 2026-11-02 (passe 2) ; 2026-11-03 (passe 5)
decision-makers: mainteneur
---
# ADR-0015 — Posture légale du dépôt public

> **Amendée le 2026-11-02 (passe 2).** En cas de divergence, la section « Amendement passe 2 » prévaut sur le texte qui précède.

> **Réécrite le 2026-11-02 (passe 1, arbitrages A17, A18, A19, A38).** La version précédente citait des noms de sites et d'outils que le projet ne doit plus publier ; elle est remplacée intégralement. Cette ADR est le seul endroit, avec la liste « Jamais » du cahier des charges et les exigences ENF-LEG, où le terme « DRM » est employé.

## Contexte et problème
Shelfhold sert à trouver, récupérer, ranger et exporter des livres, mangas et bandes dessinées pour un foyer. Une partie des contenus qui circulent en ligne est protégée par le droit d'auteur, et certains fichiers achetés sont protégés par des mesures techniques (DRM). Le dépôt est public, hébergé sur GitHub, et le mainteneur réside en France.

Précédents dans l'écosystème des lecteurs de manga et de livres numériques :
- une application de lecture de manga a arrêté son développement en janvier 2024 après une mise en demeure d'un éditeur coréen ;
- environ 200 extensions d'un fork ont été retirées en 2024 par une notification DMCA d'un ayant droit ;
- une autre application a été archivée en novembre 2025 ;
- GitHub a traité des notifications **anti-contournement** visant des dépôts d'outils de retrait de protection de livres numériques (2022, puis 2026), qui ont emporté des réseaux entiers de forks.

Les projets qui ont duré (lecteurs et serveurs qui ne livrent aucune source) partagent une règle : le dépôt ne fournit ni contenu, ni source de contenu non autorisé, ni moyen de contournement.

**Textes applicables (vérifiés le 2026-11-02 ; ceci n'est pas un avis juridique) :**
- **CPI L331-5** : protège les mesures techniques « efficaces » (code d'accès, chiffrement, brouillage, contrôle de copie) sur les œuvres autres que les logiciels ; elles ne doivent pas empêcher l'interopérabilité, l'ARCOM tranchant les litiges.
- **CPI L335-3-1** : I. le contournement sciemment d'une mesure efficace est puni de 3 750 € d'amende ; II. le fait de procurer ou proposer des moyens « conçus ou spécialement adaptés » pour contourner est puni de 6 mois d'emprisonnement et 30 000 € d'amende — ce qui inclut la fabrication, la mise à disposition « sous quelque forme que ce soit », la fourniture d'un service, et le fait d'**inciter à l'usage** ou de diffuser une publicité pour un tel moyen (4°) ; III. exception pour la sécurité informatique.
- **CPI L335-4-1** : même structure pour les droits voisins. **R335-3** : détention ou usage d'un moyen de contournement = contravention de 4e classe (rédaction consolidée actuelle non vérifiée, seule une version de 2006 a été lue).
- **Conseil constitutionnel, décision 2006-540 DC** : l'exemption « interopérabilité » a été censurée dans les articles pénaux faute de définition ; « c'est pour l'interopérabilité » n'est donc **pas** une défense légale.
- **CPI L342-1** (droit sui generis des bases de données) : interdit l'extraction substantielle, ou répétée et systématique, du contenu d'une base ; pertinent pour les fournisseurs de métadonnées sans API.
- **Directive 2001/29/CE, art. 6** : interdit aussi la **promotion** et la publicité de dispositifs de contournement.
- **17 U.S.C. § 1201** : interdit le contournement et le trafic d'outils de contournement ; l'exemption d'ingénierie inverse vise l'interopérabilité **logicielle**, pas le contenu d'un livre.

## Facteurs de décision
- Survie du dépôt, du compte et de l'organisation GitHub.
- Sécurité juridique du mainteneur (droit français en premier).
- Utilité réelle pour le foyer : des sources légales riches et un pont vers les outils que l'utilisateur installe lui-même.
- Clarté pour les contributeurs et les auteurs de plugins.

## Options considérées
### A. Dépôt neutre, sources légales seulement, aucune mention de moyen de contournement (A+B+C)
- Contenu : sources first-party légales (OPDS générique, Gutenberg via catalogues et miroirs officiels, Wikisource, Standard Ebooks, dossier d'import) ; méta-sources livrées **sans aucune source active** ; achats = uniquement des fichiers **sans DRM** déposés dans le dossier d'import ; crochet générique « dossier d'import / commande externe » documenté de façon générique ; une phrase neutre de non-prise en charge.
- Avantages : risque très faible sur L335-3-1, la directive et § 1201 ; modèle des projets qui ont duré ; message simple.
- Inconvénients : pas d'accès « hors de la boîte » aux achats protégés ; le manga demande que l'utilisateur configure lui-même son instance Suwayomi.
### B. Documenter un outil de contournement tiers et expliquer comment le brancher
- Avantages : commodité pour l'utilisateur.
- Inconvénients : risque moyen à élevé ; nommer et expliquer un outil peut constituer une **incitation** (L335-3-1 II 4°) ou une **promotion** (dir. 2001/29 art. 6(2)).
### C. Adaptateur dans le dépôt qui pilote un outil de contournement installé par l'utilisateur
- Avantages : intégration fluide.
- Inconvénients : risque élevé ; un adaptateur dont la seule fonction est de piloter un outil de contournement s'apparente à la fourniture d'un service à cette fin (3°) et constitue une cible facile pour une notification § 1201.
### D. Embarquer ou porter une logique de contournement
- Avantages : aucun.
- Inconvénients : risque très élevé ; fabrication et mise à disposition directes du moyen.
### E. Livrer des scrapers non autorisés ou un index tiers préconfiguré
- Avantages : utilité immédiate.
- Inconvénients : exposition directe ; c'est la cause des arrêts cités.
### F. Dépôt privé
- Avantages : aucune exposition publique.
- Inconvénients : contraire à l'objectif open source et à la CI publique gratuite.

## Décision
Option A.
1. **Aucune source non autorisée.** Le dépôt ne contient aucun scraper de site non autorisé, aucun index d'extensions tiers configuré, aucun indexeur torrent/Usenet (Torznab/Newznab reste « Jamais » dans le cœur), aucune bibliothèque fantôme. Les méta-sources (Suwayomi, OPDS, lncrawl) sont livrées sans aucune source active (ENF-LEG-001).
2. **Aucun moyen de contournement, aucune promotion.** Ni code, ni adaptateur, ni documentation, ni lien ne nomme ou n'explique un outil de retrait de DRM. Les achats ne sont pris en charge que sous forme de fichiers sans DRM déposés dans le dossier d'import (EF-SRC-012). Les fichiers protégés sont **détectés et refusés** avec un message, sans tentative de levée. La documentation contient une phrase neutre : « Les fichiers protégés par DRM ne sont pas pris en charge et sont refusés. »
3. **Crochet générique.** Le dossier d'import (et, plus tard, une commande externe générique) est documenté comme un mécanisme général, sans exemple visant un magasin ou un outil.
4. **Index officiel.** L'index signé des modules (M6) ne liste **aucun module de retrait de DRM** ; un module de ce type publié par un tiers n'est ni référencé, ni lié, ni discuté dans les espaces du projet (EF-SRC-017).
5. **lncrawl** (A19) : adaptateur vers **l'instance de l'utilisateur** uniquement ; Shelfhold n'active ni ne liste aucun crawler, et le compose de référence ne contient pas de profil lncrawl (EF-SRC-010).
6. **Gutenberg** : catalogues RDF/CSV officiels, miroirs ou Gutendex, jamais le site web ; agent utilisateur identifiable ; **aucun canari contre gutenberg.org** (EF-SRC-007, ENF-LEG-003).
7. **Conditions des sources et fournisseurs** tracées dans le manifest (`terms_url`, robots.txt, limites, attribution) ; aucun scraper d'une base protégée par le droit sui generis sans accord écrit (ENF-LEG-003, ENF-LEG-005).
8. **Fixtures et cassettes synthétiques ou du domaine public uniquement** (A18, ENF-LEG-002).
9. **Lint `denylist-names`** (liste privée stockée en secret CI) sur la documentation, le README, les sources et les gabarits d'issues ; gabarits demandant de retirer les URL des logs ; mention « marques de leurs titulaires, sans affiliation » (ENF-LEG-004).
10. **Procédure de notification** (A38, ENF-GOV-001) : adresse abus dédiée dans SECURITY.md et la doc ; accusé de réception sous 72 h ; pas de contre-notification sans avis juridique ; pas de réécriture d'historique ; modération légale des issues (suppression des liens vers du contenu non autorisé).
11. **Avis d'un juriste avant la 1.0 publique** (Q27 ; coupé en Q27a avant le premier commit public et Q27b avant 1.0 par l'amendement passe 5) : consultation ciblée sur L335-3-1, la portée de l'exception de licence (ADR-0014) et les conditions des fournisseurs de métadonnées.
12. Le dossier `research/` du projet n'est jamais publié.

## Conséquences
- Positives : risque juridique minimal ; règle simple pour les contributeurs (CODEOWNERS sur `sources/` et `cassettes/`) ; posture défendable devant un hébergeur.
- Négatives : refus récurrents de PR de sources et de demandes d'intégration d'achats protégés ; FAQ à maintenir ; une partie des contenus achetés n'est pas gérable par Shelfhold.

## Risque principal
Exposition malgré tout, via un module tiers populaire associé au projet, ou via une issue qui cite un site ou un outil (R3). Mitigation : aucune mention ni lien vers des dépôts tiers ; lint de noms interdits ; modération des issues ; gabarit d'issue « demande de source » qui rappelle la politique ; avis juridique avant la 1.0 publique.

## Réversibilité
Haute dans le sens restrictif ; en pratique irréversible dans l'autre sens (une fois publié, un moyen de contournement ou un scraper ne se « dé-publie » pas).

## Amendement passe 1 (2026-11-02)
Réécriture complète (A17, A18, A19, A38, confirmée par la vérification des textes en closure-A) : retrait de tous les noms de sites et d'outils de l'ancienne version et de leurs URL ; ajout des textes de loi vérifiés ; politique A+B+C (imports sans DRM, crochet générique, phrase neutre) ; index sans module de retrait ; fixtures synthétiques ; lint de noms ; procédure de notification ; avis de juriste avant la 1.0 publique.

## Amendement passe 2 (2026-11-02)
- **Fondement de l'usage et vocabulaire** (N7, B21) : la copie privée n'est licite que depuis une source licite (CJUE *ACI Adam*, C-435/12 ; CPI L122-5 2°) et pour l'usage du copiste ; la représentation dans le cercle de famille (L122-5 1°) est un régime distinct. Docs et UI parlent de « bibliothèque personnelle du foyer », jamais de « partage » au sens de diffuser (ENF-LEG-006) ; deux questions ajoutées pour le juriste (Q27). Le projet ne qualifie pas juridiquement l'usage de l'utilisateur.
- **Admission d'une source first-party** (GAP-2-23) : liste de contrôle dans le gabarit de PR (preuve d'autorisation ou licence libre, conditions et robots lus, limites, revue CODEOWNERS) ; ENF-LEG-003.
- **Textes revérifiés** (B24, `research/10-closure-C.md` §4) : R335-3 inchangé depuis le 30/12/2006 ; L335-3-1 inchangé depuis le 03/08/2006 ; L335-4-1 version du 24/10/2019 ; L331-5 version du 01/01/2022 (loi 2021-1382), sans effet sur la posture.
- **Continuité en cas de retrait** : voir ADR-0029. **Politesse HTTP** (ENF-LEG-007) et **attribution des contenus libres** (EF-BIB-033) renforcent la posture de bonne foi, comme les liens « Soutenir l'auteur » (EF-BIB-032, jamais affiliés).

## Amendement passe 3 (2026-11-02)
- **Domaine public par pays** : le vocabulaire « source légale » appliqué à Gutenberg et Standard Ebooks devient « libre de droits **aux États-Unis** » ; voir ADR-0032 (C18).
- **Fichiers tatoués** : un achat sans mesure technique peut porter un tatouage au nom de l'acheteur ; son retrait est un délit (CPI L331-11, L335-3-2) ; voir ADR-0033 (C19).
- **Bascule depuis des scripts** : la documentation de migration est générique ; aucune donnée propre à une installation n'entre dans le dépôt (C14, ADR-0034).
- **Extensions Suwayomi** : la sonde de santé alerte sur une extension non chargée mais Shelfhold ne l'installe ni ne la met à jour (C15).

## Amendement passe 4 (2026-11-03)
Cyber Resilience Act (D3, R3) : la posture légale renvoie à ADR-0037 (`docs/legal/cra.md` en M0, `SECURITY.md` complet avant 1.0, revue avant toute structure porteuse). L'avis juridique Q27 gagne un point (f) sur le CRA. Le terme « DRM » n'est employé qu'au sens juridique (mesure technique de protection), comme avant.

## Amendement passe 5 (2026-11-03)
Arbitrage E11 (GAP-5-11). L'exposition commence au **premier commit public** (ENF-LEG-010), et l'adaptateur Suwayomi y est publié dès M1a : l'avis juridique est coupé en deux.
- **Q27a, avant le premier commit public** (levé par le porteur le 2026-11-04 : publication directe sans relecture, risque accepté, 06 Q27a) : neutralité des adaptateurs vers des services connectés installés par l'utilisateur, vocabulaire de la documentation et du README, procédure de notification, DCO et exception de plugin provisoire. Responsable : mainteneur. Bloque : premier push public (M0).
- **Q27b, avant la 1.0 publique** : le reste (bibliothèque du foyer, domaine public par pays, fichiers tatoués, CGU des métadonnées, CRA).

## Amendement passe 9 (2026-11-03)
J2, J4 (X2, U2).
- La liste de contrôle d'admission (ENF-LEG-003) s'applique aussi aux **plugins de métadonnées** de l'index officiel : une notification de 2026 visant des gestionnaires de bibliothèque auto-hébergés citait précisément des plugins de métadonnées (fait public, `github/dmca`).
- Le proxy par source (EF-SRC-013, réservé) n'est **jamais** présenté comme un moyen d'atteindre un site bloqué ; les ordonnances de blocage visant des fournisseurs de VPN (France, 2025-2026) confirment la prudence de formulation.
- Un foyer peut devoir effacer tout de suite un contenu reçu d'une source tierce : `shelfhold purge --now` et les pierres tombales (ADR-0048) existent pour cela, dans un vocabulaire neutre, sans qualification juridique par le projet (ENF-LEG-006).

## Exigences liées
ENF-LEG-001, ENF-LEG-002, ENF-LEG-003, ENF-LEG-004, ENF-LEG-005, ENF-GOV-001, EF-SRC-007, EF-SRC-010, EF-SRC-011, EF-SRC-012, EF-SRC-017, EF-SRC-019, EF-SRC-024, ENF-LEG-008, ENF-LEG-009, ENF-LEG-011, ENF-LEG-010, EF-SRC-013, EF-BIB-052.

## Sources
- https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000044259273 (CPI L331-5)
- https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000006279236 (CPI L335-3-1)
- https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000038835846 (CPI L335-4-1)
- https://www.legifrance.gouv.fr/affichCodeArticle.do?idArticle=LEGIARTI000006280050&cidTexte=LEGITEXT000006069414 (CPI R335-3, version 2006)
- https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000006279166 (CPI L335-1)
- https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000006279247 (CPI L342-1)
- https://www.conseil-constitutionnel.fr/decision/2006/2006540DC.htm
- https://www.legislation.gov.uk/eudr/2001/29/article/6 (directive 2001/29/CE, art. 6)
- https://www.law.cornell.edu/uscode/text/17/1201
- https://docs.github.com/en/site-policy/content-removal-policies/guide-to-submitting-a-dmca-takedown-notice
- https://alternativeto.net/news/2024/1/manga-reader-app-tachiyomi-ceases-development-amid-legal-threats-from-kakao-entertainment
- https://torrentfreak.com/sony-dmca-notice-nukes-200-aniyomi-extensions-as-tachiyomi-fork-feels-heat-240617/
- https://f-droid.org/ja/2025/11/13/twif.html
- https://www.gutenberg.org/policy/robot_access.html
- https://standardebooks.org/feeds
- `deliverables/review/ARBITRAGES-passe3.md` (C14, C15, C18, C19)
- `deliverables/review/explorateur-passe4.md` (R3)
- https://lwn.net/Articles/1040774
- `deliverables/review/ARBITRAGES-passe4.md`
- `deliverables/review/chasseur-trous-passe5.md`
- `deliverables/review/explorateur-passe5.md`
- `deliverables/review/ARBITRAGES-passe5.md`
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- https://github.com/github/dmca (notifications publiques, 2026)
- https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000043409305 (Code pénal, art. 227-23)
