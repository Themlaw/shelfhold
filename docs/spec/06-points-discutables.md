# Shelfhold — Points discutables (v16, après revue passe 13 et arbitrages du porteur 1 et 2)

> Ce document recense les choix ouverts ou récemment tranchés. Pour chaque point, on trouve le contexte, les options (avantages et inconvénients), une **recommandation** et un **statut** : « Tranché (Axx) », avec la décision retenue, ou « Ouvert », avec le jalon avant lequel trancher. Les questions tranchées restent ici pour la traçabilité. Elles ne se rouvrent que si un fait nouveau le justifie.
> Renvois : `ADR-NNNN` (`adr/`), `EF-/ENF-` (`01-cahier-des-charges.md`), jalons M0–M6 (`03-roadmap-tdd.md`), arbitrages `Axx` (`review/ARBITRAGES-passe1.md`) et `Bxx` (`review/ARBITRAGES-passe2.md`). La passe 2 tranche Q21, complète Q3, Q8, Q15, Q17, Q26 et Q27, et ouvre Q28 à Q30. La passe 3 (arbitrages `Cxx`, `review/ARBITRAGES-passe3.md`) complète Q9, Q26, Q27 et Q30, et ouvre Q31 à Q33. La passe 4 (arbitrages `Dxx`, `review/ARBITRAGES-passe4.md`) avance Q17 (exception provisoire dès le premier commit public), complète Q8, Q25, Q27 et Q28, et ouvre Q34 et Q35. La passe 5 (arbitrages `Exx`, `review/ARBITRAGES-passe5.md`) coupe Q27 en **Q27a** (avant le premier commit public) et **Q27b** (avant 1.0), précise Q25 (format age), ouvre Q36 à Q39 et ajoute la section **Risques résiduels acceptés et responsables**. La passe 6 (arbitrages `Fxx`, `review/ARBITRAGES-passe6.md`) ouvre Q40 à Q43 et complète les risques résiduels. La passe 7 (arbitrages `Gxx`, `review/ARBITRAGES-passe7.md`) ouvre Q44 à Q47 et complète les risques résiduels. La passe 8 (arbitrages `H1`–`H4`, `review/ARBITRAGES-passe8.md`) confirme Q40, ouvre Q48 à Q52 et complète les risques résiduels. La passe 9 (arbitrages `J1`–`J5`, `review/ARBITRAGES-passe9.md`) ouvre Q53 à Q57, complète Q30 (purge immédiate, Q54) et Q45 (état `extracted_derived`, Q53), et complète les risques résiduels. La passe 10 (arbitrages `K1`–`K9`, `review/ARBITRAGES-passe10.md`) **re-tranche Q55** (K4), complète Q54 (portées et journal signé, Q60) et Q53 (original gardé en mode passage, Q62), ouvre Q58 à Q63 et complète les risques résiduels. La passe 11 (arbitrages `L1`–`L6`, `review/ARBITRAGES-passe11.md`) **re-tranche Q60** (L1 : valeurs brutes chiffrées au lieu du journal signé et des empreintes salées), complète Q54 et Q61, ouvre Q64 à Q67 et ajoute aux risques résiduels le risque accepté « trousseau perdu ⇒ journal inerte ». La passe 12 (arbitrages `M1`–`M10`, `review/ARBITRAGES-passe12.md`, première passe sans manque significatif) complète Q55, Q64 et Q66, ouvre et tranche Q68 et Q69 et ajoute le risque résiduel R66 (copies hors ligne non révocables). La passe 13 (arbitrages `N1`–`N6`, `review/ARBITRAGES-passe13.md`, deuxième passe consécutive sans manque significatif, qui **clôt la boucle de revue**) complète Q65, Q68 et Q69, ouvre et tranche Q70 et Q71 et ajoute deux risques résiduels (uid de l'hôte et CLI locale, R67 ; fenêtre entre une fuite de clé et `tombstone rekey`).

---

## Q1 — Nom du projet : Shelfhold

**Contexte.** L'ancien nom de travail est abandonné (A0) : il était pris sur PyPI et npm, un compte GitHub portait ce nom, et un moteur de livres numériques sur IPFS portait un nom voisin. Le porteur a ensuite demandé un nom anglais (2026-11-04) ; huit candidats ont été comparés et Shelfhold, libre sur PyPI, npm et GitHub, a été retenu (ADR-0019).

| Option | Avantages | Inconvénients |
|---|---|---|
| Shelfhold | Anglais ; dit le rôle d'atelier (ranger sur des étagères) ; cohérent avec le vocabulaire UI « Étagère » | Long ; `shelfhold.com` est pris par un tiers (parking) |
| shelfkeeper | Compréhensible en anglais ; même idée de rangement | Plus générique ; repli écarté |
| marque-page | Français, sympathique ; repli écarté | Évoque la lecture, qui est le rôle de Kavita (ADR-0020) ; tiret gênant dans les identifiants Python |

**Recommandation.** **Shelfhold.** Avant le premier commit public : réserver PyPI `shelfhold` et `shelfhold-sdk`, le scope npm `@shelfhold` et l'organisation GitHub ; configurer dès la réservation la **publication de confiance OIDC** (aucun jeton longue durée, passe 4, R7). Aucune recherche formelle de marque (INPI, EUIPO, USPTO) : risque accepté (voir Q27a).
**Impact d'un changement tardif.** Faible avant M0, moyen avant M6, élevé après la publication du SDK.
**Statut.** Tranché (porteur, 2026-11-04) : Shelfhold (ADR-0019).

## Q2 — SvelteKit ou React + Vite

**Contexte.** ADR-0013. SvelteKit 3.0.0 est sorti le 2026-10-01 (3.1.1 courant), avec adapter-static 4.0.1, @vite-pwa/sveltekit 2.0.0, shadcn-svelte 1.7.2 et bits-ui 2.20.3. React 19.3.1 + TanStack Router est l'alternative mûre.

| Option | Avantages | Inconvénients |
|---|---|---|
| SvelteKit 3 SPA | Moins de code ; bundle léger ; enrobage simple des lecteurs impératifs | Version majeure récente ; écosystème plus petit |
| React 19 + Vite | Écosystème, contributeurs | Plus de boilerplate ; plus lourd |
| HTMX rendu serveur (OBJ-SIM-12) | Peu de JS | Le lecteur (M5), le SSE et la file interactive le rendent inadapté ; rejet partiel (A14) |

**Décision.** SvelteKit 3 SPA, versions épinglées, repli React 19 documenté. Le périmètre UI de V1 est limité aux écrans d'atelier. Le spike S2 de M0 a 6 critères ; s'il échoue, on passe à React. La page `/lite` d'e-ink est en HTML rendu côté serveur (M5).
**Statut.** Tranché (A14), sous réserve du spike S2 (fin **M0**).

## Q3 — Huey ou runner de jobs maison

**Contexte.** ADR-0005. Faits relevés (09-closure-B) :
- Huey 3.5.4 est sorti le 2026-11-01.
- `lock_task` avec ttl n'est pas supporté sur SQLite, d'où `flush_locks` au démarrage.
- `rate_limit` fonctionne en fenêtre fixe, et le sémaphore exige Redis.
- Les alternatives sur SQLite (SAQ, Procrastinate, arq, dramatiq-sqlite, taskiq-sqlite) ne sont pas mûres.

| Option | Avantages | Inconvénients |
|---|---|---|
| Huey SqliteHuey épinglé + couche Shelfhold (slots par source/domaine, progression SSE) | Code existant, maintenu ; tâches périodiques ; priorités | Pas d'asyncio complet ; une instance par pool (io, cpu, maint) |
| Runner maison (OBJ-SIM-11) | Sur mesure | Bugs de bail et de messages toxiques à découvrir soi-même ; temps de maintenance |

**Décision.** Huey `>=3.5.4,<3.6`, accompagné d'une couche Shelfhold (table `domain_slot`, progression vers SSE, `flush_locks` au démarrage). Le spike S1 de M0 vérifie six critères :
1. 1000 tâches sans « database locked » ;
2. `kill -9` puis vidage des verrous ;
3. tâche périodique à ±2 s sur 24 h ;
4. slot de domaine à N = 2 ;
5. mise en file p99 < 5 ms ;
6. RSS au repos +20 Mo au plus.

Il n'y a pas de runner maison.

**Complément passe 2 (B24).** Le rattrapage est désormais un fait vérifié dans le code de Huey 3.5.4 : **aucun** rattrapage (crontab sans état, vérifications manquées sautées). La couche Shelfhold stocke `last_run_at`, enfile au démarrage une seule exécution par périodique en retard et rend toutes les périodiques idempotentes. Le critère 3 du spike devient : « arrêt 2 h puis redémarrage → exactement une exécution de rattrapage par périodique ». Huey figure dans `docs/dependencies.md` (facteur bus 91 %) ; le repli reste le runner maison derrière le port `JobQueue`.
**Statut.** Tranché (A12), sous réserve du spike S1 (fin **M0**).

## Q4 — Postgres

**Contexte.** ADR-0004. La nightly Postgres était chère pour un mainteneur seul (OBJ-SIM-14).

**Options.** SQLite seul, avec la portabilité comme règle de revue / SQLite + Postgres testé en nightly / Postgres dès V1.
**Décision.** SQLite seul. La nightly Postgres est retirée (ENF-PORT-002 retirée). La règle « pas de SQL spécifique hors adaptateurs » est maintenue en revue. On réévalue seulement si ENF-PERF-007 (contention) échoue sur le profil de référence ENF-PERF-008.
**Statut.** Tranché (A16).

## Q5 — Standard Ebooks : flux OPDS réservé aux patrons

**Contexte.** Seuls les flux « New Releases » sont publics. Le flux OPDS complet est un avantage réservé aux patrons (https://standardebooks.org/feeds). Les pages de chaque livre sont libres.

| Option | Avantages | Inconvénients |
|---|---|---|
| Identifiants patron par utilisateur | Respecte le modèle de financement ; OPDS propre | Seuls les patrons en profitent |
| Moissonner les pages publiques | Accessible à tous | Fragile ; contraire à l'esprit du modèle patron |
| OPDS si identifiants, sinon une URL par livre | Couvre les deux publics sans moissonnage | Deux chemins de code |

**Recommandation.** Option 3, sans parcours en masse (EF-SRC-011, ENF-LEG-003).
**Statut.** Ouvert : **M2**.

## Q6 — Achats en ligne : périmètre

**Contexte.** Le propriétaire achète des livres numériques chez des libraires en ligne. Les fichiers achetés sont souvent protégés par une mesure technique (DRM). Le contournement de ces mesures, ainsi que la mise à disposition ou la promotion de moyens pour y parvenir, est réprimé (CPI L335-3-1 et R335-3 ; directive 2001/29 art. 6 ; 17 U.S.C. §1201). L'exception d'interopérabilité a été censurée par le Conseil constitutionnel (DC 2006-540). GitHub traite les notifications anti-contournement. Le registre de recherche ne sera jamais publié. Le passage de 08-closure-A §1.6 classe les options par risque.

| Option | Avantages | Inconvénients |
|---|---|---|
| A. Importer seulement des fichiers sans DRM (dossier d'import, OPDS, téléchargements sans DRM fournis par le libraire) | Risque très faible | Une partie des achats reste hors de Shelfhold |
| B. Crochet générique « dossier d'import / commande externe », documenté sans nommer d'outil | Risque faible ; utile à d'autres usages | Le crochet ne doit jamais être présenté comme une voie de contournement |
| C. Phrase neutre dans la doc : « Les fichiers protégés par DRM ne sont pas pris en charge et sont refusés » | Risque faible ; clarté pour l'utilisateur | — |
| D. Documenter ou brancher un outil de retrait de protection, ou l'héberger dans l'organisation | Pratique pour le propriétaire | Risque moyen à élevé (incitation, mise à disposition) ; cible de notification |

**Décision.** A + B + C.
- Le dossier d'import (EF-SRC-012) refuse les fichiers protégés avec un message clair et ne tente jamais de lever la protection.
- Aucun adaptateur, aucune page de documentation et aucune issue ne nomme d'outil de contournement.
- L'index officiel de plugins ne liste aucun plugin de retrait de protection (EF-SRC-017).
- Le lint `denylist-names` contrôle la règle (ENF-LEG-004).

Ce que l'utilisateur fait chez lui de son côté ne regarde pas le projet. Le projet n'y contribue pas et n'y renvoie pas. Voir ADR-0015.
**Statut.** Tranché (A17, confirmé par 08-closure-A). L'avis juridique est traité dans Q27.

## Q7 — Lecteur intégré en V1

**Contexte.** La v1 du plan plaçait le lecteur en M3. ADR-0020 positionne Shelfhold comme l'atelier, et Kavita (Kover) comme le lieu de lecture.
**Options.** Lecteur dans V1 / lecteur secondaire en M5.
**Décision.** Le lecteur intégré devient secondaire et passe en **M5** (EF-LEC-001 à 013), avec l'OPDS et kosync propres, OIDC et `/lite`. En V1, la famille lit dans Kavita. Le schéma de progression par locator reste un crochet de M1. Son rôle exact en M5 fait l'objet de Q22.
**Statut.** Tranché (A1, A3).

## Q8 — Hardlinks et emplacement du stockage

**Contexte.** EXDEV entre montages : on ne peut ni renommer ni lier d'un montage à l'autre. ADR-0010 prévoyait un CAS avec refcount.
**Options.** Stockage dans `/data` avec export par copie / stockage dans le montage partagé avec hardlink / CAS + refcount.
**Décision.**
- Pas de CAS ni de refcount en V1 (EF-BIB-007 retirée, réservée M4). Le stockage se fait par Unité : CBZ ou EPUB tel que reçu + `unit.json` (EF-BIB-006).
- L'export se fait par **copie** par défaut. Le hardlink ou le reflink est possible en option, seulement sur le même système de fichiers et le même montage (EF-EXP-015).
- On monte le parent existant de la bibliothèque Kavita. Le staging et la corbeille se trouvent dans `/library/.shelfhold/` (ADR-0022).

**Amendement passe 2 (B6, B10–B14, ADR-0028).** Le stockage par Unité n'est plus systématique : **mode passage** par défaut pour les services connectés (le CBZ passe par le staging et va dans la bibliothèque ; Shelfhold garde `unit.json`, hash et manifeste) ; **copie conservée** en option par étagère, dans un volume **`/store`** distinct de `/data` (disque dur possible ; même système de fichiers que `/library` pour les liens durs). Fichiers adoptés référencés, non copiés. Emplacement par défaut de `/store` : voir Q29.

**Amendement passe 4 (D9, ADR-0028).** Une copie conservée ou une mise à l'abri est une **copie indépendante** (reflink si le système de fichiers le permet), **jamais un lien dur** : un lien dur partage l'inode, et une modification sur place, une troncature ou une corruption toucherait les deux noms. Le lien dur reste une option (M2, EF-EXP-015) pour le contenu **remplaçable** seulement. Le « même système de fichiers » ne sert donc plus qu'au reflink.

**Statut.** Tranché (A11, A20 ; amendé B6, B10, D9).

## Q9 — Sablier

**Contexte.** Sablier ne voit pas les appels directs de Shelfhold vers Suwayomi, et ses arrêts sont inconditionnels (09-closure-B §7).
**Options.** Shelfhold derrière Sablier / Shelfhold toujours actif, sans gestion des services tiers / Shelfhold toujours actif + port de réveil.
**Décision.** Shelfhold est **toujours actif**, jamais derrière Sablier. Le port `WakeHook` (EF-INT-022, ADR-0025) réveille Suwayomi avant un lot et renouvelle la session pendant les jobs longs. Un réveil lent n'est pas compté comme une panne de source.
**Complément passe 3 (C5, GAP-3-05).** Un Suwayomi endormi n'exécute pas sa propre planification : avec un WakeHook, la vérification des nouveautés est **pilotée** par Shelfhold (réveil puis demande de rafraîchissement des séries suivies) ; sans WakeHook, Suwayomi est déclaré « toujours actif » (EF-SRC-025).
**Statut.** Tranché (A23, C5).

## Q10 — API pour clients tiers : sous-ensemble Komga ou extension Mihon

**Contexte.** La demande la plus votée chez Suwayomi (#159) est l'intégration avec Mihon. Mihon a déjà des trackers améliorés pour `kavita`, `komga` et `suwayomi`. En V1, Kavita sert les clients tiers.

| Option | Avantages | Inconvénients |
|---|---|---|
| Sous-ensemble compatible Komga en lecture seule | Aucun travail côté client | API tierce non contractuelle |
| Extension Mihon dédiée | API sous contrôle | Code Kotlin et distribution à maintenir |

**Recommandation.** Ne rien faire tant que Kavita sert les clients. Ensuite, sous-ensemble Komga en lecture seule (EF-INT-011).
**Statut.** Ouvert : « Plus tard ».

## Q11 — Fournisseurs de métadonnées : quand

**Contexte.** La qualité des exports Kavita et le dédoublonnage dépendent des métadonnées. Les CGU des fournisseurs ont été vérifiées (08-closure-A §6.3) :
- AniList interdit son usage dans un tracker concurrent ;
- MangaUpdates exige d'être crédité ;
- Hardcover ne doit jamais être appelé depuis le navigateur.

**Options.** Tout en V2 / contrat + OpenLibrary en M2, les autres en M4 / tout en M1.
**Décision.**
- Contrat `MetadataProvider` et OpenLibrary en **M2**, avec `User-Agent` et adresse de contact.
- AniList, MangaUpdates, Hardcover et ComicVine en **M4** (EF-BIB-013).
- Respect de ENF-LEG-005 : seau à jetons par fournisseur, cache, attribution dans l'UI.
- BDGest : pas de scraper dans le dépôt.

**Statut.** Tranché (A7, A3).

## Q12 — PWA hors ligne

**Contexte.** La lecture hors ligne en V1 passe par Kavita, Kover et KOReader.
**Options.** PWA en V1 / PWA en M5 avec le lecteur.
**Décision.** PWA hors ligne en **M5** (EF-LEC-012). L'interface d'atelier en V1 n'en a pas besoin.
**Statut.** Tranché (A3).

## Q13 — lncrawl : serveur ou paquet importé

**Contexte.** lncrawl est sous GPL-3.0. L'import en processus violerait EF-SRC-002 (« aucune I/O directe par un plugin »).
**Options.** Instance hors processus pilotée par l'utilisateur / import du paquet.
**Décision.** **Adaptateur vers l'instance de l'utilisateur** (EF-SRC-010), avec l'EPUB transmis tel quel. Shelfhold n'active ni ne liste aucun crawler par défaut. Le compose fourni ne contient pas de profil lncrawl.
**Statut.** Tranché (A19).

## Q14 — Organisation GitHub ou compte personnel

**Contexte.** La merge queue n'existe que pour les dépôts d'organisation. Les rulesets fonctionnent sur les dépôts personnels publics (09-closure-B §5).
**Options.** Compte personnel / organisation gratuite.
**Décision.** **Organisation gratuite** dès M0. L'image est `ghcr.io/<org>/shelfhold`, et les commits utilisent l'e-mail `noreply` (ADR-0026).
**Statut.** Tranché (A16).

## Q15 — Suwayomi : pages ou CBZ

**Contexte.** EF-SRC-008.
**Options.** Récupération page par page / CBZ via `GET /api/v1/chapter/{id}/download`.
**Décision.** **CBZ par défaut**. Pas de mode page par page en V1 : Suwayomi gère sa file, ses réessais et son solveur. Le doublon sur disque côté Suwayomi est documenté. Sa purge est une option à vérifier sur la version épinglée.
**Complément passe 2 (B6, B16).** Le spike **S5** (M0) vérifie contre un Suwayomi réel que la route renvoie bien une archive CBZ et que la suppression d'un chapitre téléchargé existe ; la purge après export réussi est une option par étagère, **désactivée par défaut**. AuthFlow Basic est disponible dès M1 (M1a). **Complément passe 4 (R7, D6, D13)** : S5 vérifie aussi le chemin **GraphQL** équivalent au téléchargement CBZ (l'API REST pourrait être retirée), les mutations d'ajout et de retrait d'une série dans la bibliothèque et d'affectation de catégorie (EF-SRC-029), et la réponse à un mot de passe changé (401).
**Statut.** Tranché (A2), sous réserve du spike S5.

## Q16 — OIDC

**Contexte.** Un reverse proxy d'authentification peut se trouver en frontal, mais aucun n'est supposé (arbitrage porteur P5). Kavita reste l'authentification de lecture en V1.
**Options.** OIDC en V1 / OIDC en M5.
**Décision.** En V1 : admin local, clés API scopées, forward-auth **désactivé par défaut**, CIDR obligatoire une fois activé, secret partagé recommandé (EF-USR-002, amendé par P5, Q72). OIDC arrive en **M5** (EF-USR-004).
**Statut.** Tranché (A15).

## Q17 — Formulation de l'exception de plugin AGPL

**Contexte.** ADR-0014 prévoit une application AGPL-3.0-or-later, un SDK MIT et une exception pour les plugins chargés en processus. L'exception ne couvre que le code détenu par le projet (A19, OBJ-LEG-07). Aucun juriste n'a relu le texte.

| Option | Avantages | Inconvénients |
|---|---|---|
| Exception rédigée maison | Rapide | Formulation ambiguë |
| Exception de liaison existante, adaptée | Texte éprouvé | Adaptation nécessaire |
| Pas d'exception, isolation hors processus en M6 | Rien à rédiger | Plugins en processus contraints à l'AGPL |

**Recommandation (révisée passe 2, B24).** Partir du modèle FSF « linking over a controlled interface » (permission additionnelle §7) et du précédent CondationCMS : modules qui communiquent uniquement par l'API plugin publique (paquet SDK MIT), retirables sans modifier le cœur, API internes exclues, exception caduque si l'interface est modifiée, limitée au code détenu par le projet. **Ne pas la publier avant la relecture** : jusque-là, `LICENSE` reste AGPL-3.0-or-later + SDK MIT, ce qui est sûr (ajouter une permission est facile, la retirer est impossible pour les versions déjà distribuées). Repli sans exception : modèle Grafana. Contributions sous DCO, sans CLA. Relecture à regrouper avec Q27.
**Amendement passe 4 (D2, ADR-0036).** La recommandation « ne pas publier avant relecture » est **renversée**. Une permission additionnelle ne peut être accordée que par les titulaires des droits : dès la première contribution externe fusionnée, ajouter l'exception exigerait l'accord de chaque contributeur. À l'inverse, une permission additionnelle peut toujours être retirée par la suite (AGPL §7). D'où : **premier commit public** avec `LICENSE` AGPL + exception de plugin **provisoire** (en-tête « texte relu avant 1.0 et avant M6 »), **DCO obligatoire** et clause entrante dans `CONTRIBUTING.md` (ENF-LEG-010). La relecture (regroupée avec Q27) peut ensuite resserrer ou préciser le texte.
**Impact d'un changement tardif.** Faible pour un resserrement ; élevé pour un ajout après la première contribution externe (d'où la décision immédiate).
**Statut.** Tranché sur le principe (D2) : exception provisoire et DCO dans le **premier commit public (M0)** ; relecture juridique ouverte, avant **1.0** puis avant **M6**.

## Q18 — Directions de design et densité

**Contexte.** Le périmètre UI de V1 se limite aux écrans d'atelier (A14). Les palettes sont fixées au niveau AA : ocre ≈ `#A3471F`, gris ≈ `#6E6357` (A32). Le thème e-ink de la SPA est rétrogradé ; `/lite` le remplace (M5).

| Option | Avantages | Inconvénients |
|---|---|---|
| Une direction + un réglage de densité pour l'admin | Une seule surface à tester | Moins de personnalité pour les sections manga |
| Plusieurs directions (bibliothèque, console, galerie, calme) | Couvre tous les personas | Quatre jeux de styles pour un mainteneur seul |

**Recommandation.** Une seule direction et un seul système de tokens, avec une densité « compacte » pour l'admin (Activité, recherche). Les cartes de galerie restent un composant. Le script de contraste des tokens est bloquant (ENF-A11Y-001).
**Statut.** Tranché par le porteur (arbitrage porteur 2, Q2 ; voir Q74) : quatre thèmes sur un socle unique, A « Bibliothèque » par défaut ; la 1.0 ne livre que A (la recommandation ci-dessus vaut pour la livraison de M3), B, C, D en M4.

## Q19 — Series ou Work : faut-il une entité au-dessus de l'Œuvre ?

**Contexte.** BD franco-belge par albums, séries de romans et intégrales (OBJ-EVO-10).
**Options.** Entité Series optionnelle / tags et collections / rien.
**Décision.**
- Une entité **Series** optionnelle au-dessus de Work (EF-BIB-025, ADR-0021), avec ordre de lecture.
- À l'export Kavita, la série Kavita correspond à la Series si elle existe, sinon à l'Œuvre.
- Les intégrales passent par `unit_coverage` (passe 6, EF-BIB-044, qui remplace `content_coverage`), usage réservé à V2.
- Les identifiants externes et les contributeurs sont polymorphes sur Series, Work, Edition et Volume.

**Statut.** Tranché (A6).

## Q20 — Compatibilité du contrat de plugin (`api`)

**Contexte.** Le SDK reste interne en V1 et sera publié en M6 (ADR-0024). Une rupture du contrat casse les plugins tiers.

| Option | Avantages | Inconvénients |
|---|---|---|
| Semver strict, aucune compatibilité arrière | Code simple | Chaque rupture casse tout l'écosystème |
| Support de N et N-1 via un shim, état de plugin versionné | Fenêtre de migration pour les tiers | Un shim à maintenir |
| Support de N, N-1 et N-2 | Confort maximal | Coût de maintenance excessif pour un mainteneur seul |

**Décision.** Principe N-1 via un shim, avec un état de plugin versionné (`memo_version`, `payload_schema`).
**Recommandation.** Support de N-1 pendant **12 mois** après la sortie de N, annoncé dans la politique de versions (ENF-OPS-008).
**Statut.** Tranché sur le principe (A9) ; la durée est ouverte jusqu'à la publication du SDK (**M6**).

## Q21 — Propriétaire de la synchronisation Kobo

**Contexte.** Kavita propose déjà des fonctions pour les liseuses, et Komga et Calibre-Web émulent le protocole de synchronisation Kobo. Ce protocole n'est pas documenté par Kobo (08-closure-A §2). La surface minimale comprend :
- `initialization` ;
- `auth/device` factice ;
- `library/sync`, avec le jeton `x-kobo-synctoken` ;
- `library/{id}/state` ;
- le téléchargement avec conversion kepubify ;
- un jeton par appareil.

| Option | Avantages | Inconvénients |
|---|---|---|
| Shelfhold émule la synchronisation (EF-INT-012, M6) | Contrôle des étagères et de la progression ; un jeton par appareil | Protocole fragile à maintenir ; recoupe Kavita |
| Laisser la synchronisation à Kavita ou Komga (destination) | Rien à maintenir | Dépend des fonctions de la destination |
| Destination Kobo par fichiers (EF-EXP-021, kepubify) seulement | Simple | Pas de progression |

**Décision (passe 2, B18).** **Laisser la synchronisation Kobo à Kavita**, qui l'a annoncée pour 2026 ; la revue de chevauchement de chaque jalon (ENF-GOV-003) suit son avancement. Shelfhold ne l'émule en M6 (EF-INT-012, conditionnelle) **que si Kavita ne l'a pas livrée** d'ici là ; dans ce cas : surface minimale, modèle SyncPoint, jeton par appareil, proxy magasin **désactivé par défaut, opt-in par appareil**, avec avertissement (les CGU du magasin Kobo n'ont pas pu être lues, `research/10-closure-C.md` §6).
**Statut.** Tranché (B18) ; condition réévaluée à la sortie de M5.

## Q22 — Rôle du lecteur intégré en M5

**Contexte.** Le lecteur est secondaire (ADR-0020). Deux usages sont plausibles : un aperçu ou un extrait avant d'ajouter, et la lecture d'œuvres non exportées.

| Option | Avantages | Inconvénients |
|---|---|---|
| Aperçu seulement (quelques pages, pas de progression) | Petit périmètre | Peu utile hors atelier |
| Lecteur complet avec progression synchronisée avec Kavita | Lecture sans Kavita | Deux lieux de lecture, conflits de progression |
| Lecteur complet seulement pour le contenu non exporté | Pas de concurrence avec Kavita | Règle à expliquer |

**Recommandation.** Option 3, avec « Lire un extrait » pour tout contenu.
**Statut.** Ouvert : début de **M5**.

## Q23 — Étagère par Œuvre ou par Édition

**Contexte.** Une même Œuvre peut exister en manga FR et en roman EN (OBJ-UX-05, OBJ-EVO-21).
**Options.** Par Œuvre / par Édition.
**Décision.** **Par Édition**, avec une valeur par défaut tirée du `kind`. Une Édition figure au plus dans une étagère par bibliothèque distante (EF-EXP-002).
**Statut.** Tranché (A31).

## Q24 — OPDS propre exposé à la famille (M5)

**Contexte.** En V1, l'OPDS de Kavita sert les liseuses. À partir de M5, Shelfhold a son propre OPDS (EF-INT-007).

| Option | Avantages | Inconvénients |
|---|---|---|
| Exposé à tous les profils | Accès au contenu non exporté | Deux catalogues OPDS ; contrôle parental à prouver sur une surface de plus |
| Masqué par défaut, activable par l'admin | Un seul catalogue pour la famille | Moins de fonctions visibles |

**Recommandation.** Masqué par défaut. L'activer impose la suite de tests de contrôle parental (ENF-SEC-010).
**Statut.** Ouvert : **M5**.

## Q25 — Clé maîtresse absente au premier démarrage

**Contexte.** La clé chiffre les secrets (ENF-SEC-005). Si elle est perdue, la sauvegarde ne sert à rien (ADR-0023).

| Option | Avantages | Inconvénients |
|---|---|---|
| Refuser de démarrer sans secret fourni | Explicite | Obstacle à l'installation (O8) |
| Générer la clé dans `/data` (droits 0400) et forcer son export dans l'assistant | Installation fluide | La clé vit à côté des données tant qu'elle n'est pas exportée |

**Recommandation.** Générer la clé et forcer son export, chiffré par une phrase de passe, dans l'assistant (EF-UX-001). Afficher une alerte persistante tant que l'export n'est pas fait. **Passe 4 (GAP-4-14, GAP-4-17)** : avant l'assistant, `shelfhold secrets export` (M1a) exporte la clé chiffrée par une phrase de passe ; la sauvegarde intégrée (M1b) lit sa phrase de passe dans `SHELFHOLD_BACKUP_PASSPHRASE_FILE` et refuse de produire une sauvegarde en clair. **Passe 5 (GAP-5-17)** : le format est fixé : fichier **age v1** (destinataire scrypt dérivé de la phrase de passe lue par `--passphrase-file`, armure ASCII), jamais vers la sortie standard sans `--out -` explicite, identique à celui de la clé incluse dans la sauvegarde de M1b ; en M1a, la doc fait sauvegarder `/data` chaque jour (RPO 24 h).
**Statut.** Ouvert : **M1a** (CLI, format tranché) / M3 (assistant).

## Q26 — Comptes Kavita par membre de la famille

**Contexte.** La correspondance entre comptes Shelfhold et Kavita (EF-USR-012) est prévue en M4. Elle sert à la synchronisation de progression par personne.

| Option | Avantages | Inconvénients |
|---|---|---|
| Un jeton Kavita par utilisateur et par destination (chiffré) | Progression par personne, restrictions par compte | Configuration par membre |
| Un seul compte Kavita de service | Simple | Pas de progression par personne |

**Recommandation.** Un jeton par utilisateur, saisi par l'admin ou par le membre. Le compte de service reste réservé aux opérations d'export.
**Complément passe 2 (B1, B15, ADR-0027).** Dès M1 (M1b depuis la passe 4, avec le contrôle parental), chaque enfant doit avoir son **propre compte Kavita**, limité aux bibliothèques famille : c'est la barrière du contrôle parental, indépendamment des jetons. Les jetons par membre (M4) servent aussi à préserver la progression de chacun lors des renommages (EF-EXP-017, EF-EXP-030). L'audit des comptes Kavita (EF-EXP-036) est en M3.
**Complément passe 3 (C10, GAP-3-10).** L'audit M3 n'attend pas les jetons : une correspondance **déclarative** profil ↔ nom d'utilisateur Kavita, sans jeton, est livrée en M3 (EF-USR-017) ; schéma conseillé : une bibliothèque Kavita par palier d'âge utilisé.
**Statut.** Ouvert : **M4** (jetons) ; comptes enfants dédiés tranchés (B1) ; correspondance déclarative tranchée (C10).

## Q27 — Avis d'un juriste en deux temps (Q27a avant le premier commit public, Q27b avant la 1.0 publique)

**Contexte.** Les textes ont été vérifiés dans les sources primaires (08-closure-A §1) : CPI L331-5, L335-3-1, L335-4-1, R335-3 ; DC 2006-540 ; directive 2001/29 art. 6 ; 17 U.S.C. §1201 ; CPI L342-1 pour les bases de données. Cette vérification n'est pas un avis juridique. Trois sujets restent à faire relire :
- la politique sur les DRM (Q6, ADR-0015) ;
- l'exception de licence (Q17) ;
- les CGU des fournisseurs de métadonnées (ENF-LEG-005) et de BDGest ;
- **(ajout passe 2, B21, N7)** le cadre de la **bibliothèque personnelle du foyer** : (a) une copie faite par l'utilisateur depuis une source licite, puis lue par les membres de son foyer via Kavita, relève-t-elle de la copie privée (CPI L122-5 2° ; CJUE *ACI Adam*, C-435/12 : copie depuis une source licite seulement) ou de la représentation dans le cercle de famille (L122-5 1°), et à quelles conditions (même foyer, pas d'accès hors du cercle) ? (b) le projet doit-il limiter techniquement le nombre de comptes ou l'accès hors du réseau local, ou une documentation suffit-elle ? (c) le vocabulaire retenu (« bibliothèque personnelle du foyer », jamais « partager ») est-il adéquat ?
- **(ajout passe 3, C18, C19)** (d) **domaine public par pays** (ADR-0032) : un avertissement indicatif non bloquant suffit-il quand une source libre de droits aux États-Unis propose une œuvre encore protégée en France (prorogations de guerre, « mort pour la France », droits du traducteur) ? Le vocabulaire « libre de droits aux États-Unis » est-il adéquat ? (e) **fichiers tatoués** (ADR-0033) : la réécriture ciblée de l'OPF, toutes autres entrées copiées octet pour octet, respecte-t-elle CPI L331-11 ? L'exclusion des diagnostics et des destinations hors du foyer est-elle suffisante au regard des données personnelles de l'acheteur ?
- **(ajout passe 4, D3, ADR-0037)** (f) **Cyber Resilience Act** (règlement UE 2024/2847) : le projet, mainteneur individuel sans monétisation, est-il bien hors champ ? Quels faits le feraient basculer (dons, hôte fiscal, association, support payant, redistribution par un vendeur de NAS) ? Une association porteuse serait-elle « steward » au sens de l'art. 24, et avec quelles obligations (politique de cybersécurité, signalement des vulnérabilités activement exploitées depuis le 11 septembre 2026) ? `docs/legal/cra.md` sert de support ; voir Q35.

| Option | Avantages | Inconvénients |
|---|---|---|
| Consultation ciblée (avocat spécialisé ou association du logiciel libre) avant la 1.0 publique | Réduit l'exposition personnelle du mainteneur | Coût ou délai |
| Pas de consultation ; politique A + B + C seulement | Rien à payer | Risque résiduel non évalué |
| Consultation seulement si une notification arrive | Coût différé | Réaction dans l'urgence |

**Passe 5 (E11, GAP-5-11) : avis en deux temps.** Le dépôt est public dès le premier commit (ENF-LEG-010) et l'adaptateur Suwayomi y est publié dès M1a ; l'exposition commencerait donc environ un an avant un avis rendu à la fin de M3.

### Q27a — Avant le premier commit public (M0)
- Neutralité des **adaptateurs vers des services connectés** installés par l'utilisateur (Suwayomi, lncrawl, OPDS) : aucun collecteur livré, aucun nom de source, vocabulaire de la doc et du README.
- **Procédure de notification** (accusé 72 h, pas de contre-notification sans avis, pas de réécriture d'historique).
- **DCO et exception de plugin provisoire** (ADR-0036, Q17) : rédaction suffisante pour un premier commit ?
- Responsable : **mainteneur**. Levée le 2026-11-04 : ne bloque plus le premier push public (publication directe, risque accepté).

### Q27b — Avant la 1.0 publique (fin M3)
- Les autres sujets : DRM (Q6) et posture d'ensemble, bibliothèque personnelle du foyer (a–c), domaine public par pays (d), fichiers tatoués (e), Cyber Resilience Act (f), CGU des fournisseurs de métadonnées et de BDGest, relecture finale de l'exception (Q17).
- Responsable : **mainteneur**. Bloque : l'annonce publique de la 1.0 (porte P3).

**Recommandation.** Consultation ciblée en deux temps (Q27a puis Q27b), avec la politique écrite (ADR-0015, ENF-LEG-001 à 007) comme support. Regrouper Q27b avec la relecture de Q17. Textes revérifiés le 2026-11-02 (`research/10-closure-C.md` §4) : R335-3 et L335-3-1 inchangés depuis 2006, seul L331-5 modifié (2021), sans effet sur la posture.
**Statut.** Q27a : levée par décision du porteur (2026-11-04) : publication directe sans relecture juridique ; risque accepté et consigné (section « Risques résiduels acceptés »). Q27b reste ouvert : avant la 1.0 publique (fin **M3**).

## Q28 — Second registre d'images (résilience)

**Contexte.** ADR-0029, ENF-GOV-004 (B19) : la CI publie les images sur GHCR **et** un second registre, avec un miroir du dépôt hors GitHub. Reste à choisir le second registre.

| Option | Avantages | Inconvénients |
|---|---|---|
| Docker Hub (organisation gratuite, programme open source) | Nom familier, accessible partout, compatible avec tous les outils | Limites de téléchargement pour les anonymes ; politique de retrait comparable à GitHub ; dépend d'un acteur commercial |
| Registre Codeberg (Forgejo) | Hébergeur associatif européen ; registre et miroir du dépôt au même endroit | Quotas et disponibilité plus modestes ; politique d'hébergement de projets « à risque » à vérifier |
| Registre Forgejo auto-hébergé | Maîtrise totale | Disponibilité dépendante du mainteneur ; bande passante |

**Recommandation.** Codeberg pour le miroir du dépôt **et** son registre (cohérence, indépendance vis-à-vis de GitHub), Docker Hub en option si les quotas Codeberg posent problème. La signature cosign rend l'hôte interchangeable.
**Statut.** Ouvert : avant **M1b** (premier workflow `publish-mirror`).

## Q29 — Emplacement par défaut de `/store`

**Contexte.** ADR-0028, ENF-OPS-012 (B10) : `/data` (base, SSD local) et `/store` (copies conservées) sont séparés. Le compose de référence doit proposer un emplacement par défaut pour `/store`.

| Option | Avantages | Inconvénients |
|---|---|---|
| `/store` à côté de `/library`, sur le même disque dur (ex. `/srv/media/shelfhold-store`) | Liens durs possibles avec la bibliothèque (même système de fichiers) ; `/data` reste petit | Le disque des médias est sollicité (fenêtre de maintenance nécessaire) |
| `/store` sous `/data` (même SSD) | Un seul chemin à configurer | SSD rempli par les copies ; pas de liens durs vers `/library` (EXDEV) ; contredit N5 |
| Pas de défaut : obligatoire à la première configuration | Choix conscient | Friction à l'installation ; le mode passage n'en a pas besoin |

**Recommandation.** Défaut à côté de `/library` sur le disque des médias (première option), mais `/store` **facultatif** tant qu'aucune étagère n'est en copie conservée : l'assistant ne le demande que si l'utilisateur active la copie conservée ou ajoute une source native.
**Statut.** Ouvert : avant la fin de **M0** (disposition des volumes figée).

## Q30 — Mode passage par défaut

**Contexte.** GAP-2-10 : le contenu existait jusqu'à trois fois sur le disque. ADR-0028.

| Option | Avantages | Inconvénients |
|---|---|---|
| Mode passage par défaut pour les services connectés, copie conservée en option | Une copie de moins ; `/store` vide pour la plupart des familles | Réparation par re-téléchargement seulement |
| Copie conservée par défaut partout | Réparation locale | Disque ×2 à ×3 |

**Décision.** Mode passage par défaut pour les services connectés (Suwayomi) ; copie conservée par défaut pour les sources natives et le dossier d'import ; purge Suwayomi après export réussi en option, **désactivée par défaut** ; drapeau `irreplaceable` avec suggestion de copie conservée.
**Complément passe 3 (C6, GAP-3-06).** « Mettre à l'abri » : avant toute purge, si la source est fragile ou disparue ou si l'Unité est candidate « irremplaçable » (dès 2 échecs consécutifs), le fichier est copié dans `/store` ; purge permise seulement si (copie Kavita) ∧ (copie `/store` ∨ source vivante) (EF-BIB-038).
**Statut.** Tranché (B6, C6).
**Passe 9 (J2).** La purge Suwayomi ordinaire reste soumise à « Mettre à l'abri » et à I11 ; seule `shelfhold purge --now` (Q54, EF-BIB-052) passe outre, pour les copies gérées.

## Q31 — Conditions d'usage de Gallica, Bibebook et Ebooks libres et gratuits

**Contexte.** Le §5.3 de `01-cahier-des-charges.md` liste ces trois catalogues francophones comme sources natives possibles (« Plus tard », plugins V2 ou M4). Leurs conditions d'usage n'ont pas été relues : réutilisation commerciale ou non, attribution exigée, limites de débit, acceptation du téléchargement automatisé. ENF-LEG-003 exige une URL de conditions dans le manifeste de chaque source et ADR-0032 une base de droits (`rights_basis`).

| Option | Avantages | Inconvénients |
|---|---|---|
| Relire les conditions de chacun avant d'écrire le plugin, consigner `rights_basis`, attribution et débit dans le manifeste | Conforme à ENF-LEG-003 ; pas de surprise pour les utilisateurs | Un peu de travail par source |
| Écrire les plugins puis vérifier | Plus rapide | Risque de retrait ou de blocage d'accès |
| Ne proposer que Gutenberg et Standard Ebooks | Aucun travail | Peu de contenu francophone |

**Recommandation.** Relecture avant tout plugin, résultat consigné dans `docs/sources/` et le manifeste ; Gallica d'abord (volume francophone), avec attention à la mention de source et à la réutilisation commerciale, qui ne concerne pas un usage domestique.
**Statut.** Ouvert : avant le premier plugin francophone (**M4**, ou plugin V2 si plus tôt).

## Q32 — Pays par défaut de l'instance

**Contexte.** ADR-0032 et EF-SRC-024 : le domaine public s'apprécie par pays. Il faut une valeur par défaut pour `instance.jurisdiction`.

| Option | Avantages | Inconvénients |
|---|---|---|
| Déduite de la locale (`fr-FR` → FR), affichée et confirmée dans l'assistant | Juste dans la plupart des cas ; un clic | Locale technique parfois anglaise par défaut |
| Toujours demandée, sans défaut | Choix conscient | Une question de plus à l'installation |
| États-Unis par défaut | Cohérent avec Gutenberg | Trompeur pour un foyer français |

**Recommandation.** Déduite de la locale, **confirmée** dans l'assistant ; si la locale ne désigne pas de pays (`C`, `en`), la question est posée sans défaut. Modifiable ensuite dans les réglages.
**Statut.** Ouvert : avant **M2** (premières sources natives).

## Q33 — Empaquetage des romans en cours de parution

**Contexte.** Un roman web publié chapitre par chapitre (lncrawl) donnait un EPUB entier réécrit à chaque chapitre : progression KOReader perdue, fichier remplacé dans Kavita (P7). L'arbitrage C20 fixe la politique de tomes (`edition.volume_policy`).

| Option | Avantages | Inconvénients |
|---|---|---|
| `closed_only` : seuls les tomes fermés sont empaquetés et exportés, jamais réécrits | Fichiers stables, progression préservée | Les derniers chapitres attendent la fermeture du tome |
| `rolling_tail` : tomes fermés + une queue « en cours » remplacée au même chemin | Lecture à jour | Progression de la queue perdue à chaque remplacement (avertissement) |
| `whole` : un seul EPUB | Simple | Réservé aux œuvres finies |

**Décision.** `closed_only` par défaut, `rolling_tail` en option par Édition, `whole` pour les œuvres finies (C20, EF-BIB-040).
**Reste ouvert.** Taille d'un tome quand la source n'en donne pas (nombre de chapitres fixe, 50 par défaut proposé, ou découpage par volume de texte).
**Statut.** Tranché sur la politique (C20) ; taille par défaut ouverte : avant **M2**.

---

## Q34 — Budget de temps du mainteneur (passe 4)

**Contexte.** L'explorateur de la passe 4 (R1) estime la 1.0 à ~650–1 070 h et la roadmap entière à ~1 110–1 770 h (`03-roadmap-tdd.md` §1 bis). Le découpage M1a / M1b, les boîtes de temps et la règle de coupe (ADR-0035, ENF-GOV-005) supposent un nombre d'heures par semaine que seul le mainteneur peut fixer.

| Option | Avantages | Inconvénients |
|---|---|---|
| 6 h par semaine | Soutenable à long terme | M1a en ~6 mois, 1.0 en ~2 à 3 ans |
| 10 h par semaine | 1.0 en ~1,5 an | Risque d'épuisement ; peu compatible avec une charge de maintenance après la publication |
| Pas d'hypothèse | Rien à décider | Vélocité sans référence, règle de coupe inapplicable |

**Recommandation.** Partir de **6 h par semaine** (hypothèse basse), l'inscrire dans `ROADMAP.md` en M0 et la réviser à la revue de M1a à partir de la vélocité mesurée. Ne pas annoncer de date publique.
**Statut.** Ouvert : à fixer en **M0** (avant le premier commit de M1a).

## Q35 — Structure porteuse et Cyber Resilience Act (passe 4)

**Contexte.** La gouvernance (B23) envisage un hôte fiscal ou une association pour recevoir des dons. Le CRA (règlement UE 2024/2847) laisse hors champ un mainteneur bénévole qui ne monétise rien, mais une personne morale qui soutient durablement un logiciel libre destiné à des activités commerciales peut devenir **steward** (art. 24) et un intégrateur commercial devient fabricant (`review/explorateur-passe4.md` R3 ; ADR-0037).

| Option | Avantages | Inconvénients |
|---|---|---|
| Rester sans structure (mainteneur individuel, aucun don) | Hors champ le plus clair | Pas de financement |
| Hôte fiscal existant (type Open Collective Europe) | Pas de personne morale propre | Statut CRA à confirmer *[non vérifié]* |
| Association loi 1901 dédiée | Contrôle, financement | Peut devenir steward : politique de cybersécurité, coopération, signalement |

**Recommandation.** Aucune structure avant 1.0. Avant toute création ou tout rattachement : relire `docs/legal/cra.md`, poser la question à l'avis juridique (Q27 point f) et consigner la décision dans un ADR.
**Statut.** Ouvert : à trancher **avant toute structure porteuse** (au plus tôt après 1.0).

## Q36 — Boîte de temps de M1a après la passe 5

**Contexte.** La passe 5 ajoute 13 exigences à M1a (configuration headless, rapatriement, nom figé, filigrane, préflight, audit Kavita, détachement testé, `ui_login`, fraîcheur, garde de migration, statistiques). La ré-estimation de bas en haut donne 170–250 h (`03-roadmap-tdd.md` §1 bis). GAP-5-10 et E10 disent de revoir la boîte, pas les garanties.

| Option | Avantages | Inconvénients |
|---|---|---|
| Garder 150 h | Pression sur le périmètre | Dépassement quasi certain ; la règle de coupe deviendrait la norme |
| **Passer à 200 h** (seuil de coupe 300 h), coupables inchangés | Boîte plausible, intouchables protégés | Premier usage réel plus tard (4 à 10 mois à 6–10 h par semaine) |
| Déplacer des exigences de bascule vers M1b | M1a plus court | M1a ne remplacerait plus une automatisation existante sans risque (O9) |

**Recommandation.** 200 h ; réviser à la revue de la 4e semaine de M1a avec la vélocité mesurée ; si la projection dépasse 300 h, appliquer la coupe aux seuls coupables puis revoir l'hypothèse d'heures (Q34).
**Statut.** Ouvert : à confirmer en **M0** avec Q34.

## Q37 — Seuil de la porte P2 et contenu de M3-lite

**Contexte.** La porte P2 (ADR-0038) décide avant M3 si la partie famille mérite ses 240 à 420 h. Le signal vient d'un seul foyer (test concierge de 8 semaines, prototype à 3 personnes).

| Option | Avantages | Inconvénients |
|---|---|---|
| **≥ 2 demandes par mois et au moins une personne qui préfère l'UI** | Simple, mesurable | Arbitraire ; un foyer peu demandeur fait basculer en M3-lite |
| Seuil plus haut (≥ 4 par mois) | Évite une UI peu utilisée | Risque de sous-investir dans le contrôle parental |
| Pas de porte | Rien à mesurer | Risque d'UI inutile (R37) |

**Recommandation.** Seuil de 2 par mois ; M3-lite garde **toujours** le contrôle parental complet et l'audit des comptes (sécurité), et ne reporte que le parcours « Demander », « Mes demandes » et la recherche famille.
**Statut.** Ouvert : à trancher **avant M3** (porte P2).

## Q38 — Prise en charge du mode multi-comptes de Suwayomi (`ui_login`)

**Contexte.** La PR Suwayomi #623 (comptes utilisateurs) est fusionnée et arrive avec la prochaine stable (T1). En `ui_login`, la bibliothèque est par compte, les téléchargements sont partagés à compteur de références et l'authentification passe par JWT.

| Option | Avantages | Inconvénients |
|---|---|---|
| Refuser `ui_login` en M1a, compte de service + JWT en M1b | Pas de suivi partiel silencieux ; un seul flux à écrire en M1a | Un foyer déjà en `ui_login` attend M1b ou repasse en `basic_auth` |
| Prendre en charge `ui_login` dès M1a | Couvre tout de suite les foyers multi-comptes | Ajoute 8 à 12 h à une boîte déjà serrée ; API encore jeune |
| Ne jamais le prendre en charge | Rien à faire | Les foyers visés basculeront justement en `ui_login` |

**Recommandation.** Refus en M1a, prise en charge en M1b (E12) ; revoir si la stable sort avant la fin de M0 (porte P0).
**Statut.** Tranché sur le principe (E12) ; date de la stable à surveiller (`docs/dependencies.md`).

## Q39 — Fenêtre de maintenance par défaut

**Contexte.** La fenêtre de 2 h (03 h–05 h) ne contenait pas ses jobs sur le profil de référence (GAP-5-09).

| Option | Avantages | Inconvénients |
|---|---|---|
| **4 h (03 h–07 h), jobs ordonnés par priorité, interruptibles et repris la nuit suivante** | Les budgets tiennent ; aucune opération tronquée | Disques réveillés plus longtemps ; chevauche parfois une lecture matinale |
| 2 h, budget d'intégrité exprimé en part de la fenêtre | Disques plus souvent en veille | Intégrité complète plus lente à boucler |
| Pas de fenêtre | Simple | Contraire à l'objectif de veille des disques (ENF-OPS-014) |

**Recommandation.** 4 h par défaut (E9), fixe en M1a, réglable à partir de M1b ; la page « NAS et économie d'énergie » explique comment la raccourcir.
**Statut.** Tranché (E9).

## Q40 — Identifiant Shelfhold dans les fichiers exportés

**Contexte.** Inscrire un identifiant d'`export_item` dans ComicInfo (`Notes`) ou dans l'OPF est une décision à sens unique : un fichier exporté sans identifiant ne peut en recevoir un que par réécriture, ce qui réinitialise la position KOReader (GAP-6-16, passe 6).

| Option | Avantages | Inconvénients |
|---|---|---|
| `<Notes>shelfhold:item=<ulid></Notes>` / `shelfhold:id` dans l'OPF | Reconnaissance d'un fichier déplacé ou restauré sans manifeste | Trace de l'outil dans chaque fichier (contraire à ADR-0030) ; recopiée par les outils tiers ; champ que Kavita ou l'utilisateur peut écraser |
| **Aucun identifiant** : manifeste, hash et chemin | Empreinte minimale, discrétion ; le manifeste est sauvegardé (ENF-OPS-020) | Un fichier déplacé à la main sans manifeste se reconnaît par hash seulement (`reconcile --adopt-after-restore`) |

**Recommandation.** Aucun identifiant (ADR-0040). Revoir seulement si la reconnaissance par hash échoue sur des cas réels de restauration.
**Statut.** Tranché (passe 6, ADR-0040) ; **confirmé en passe 8** (H1) : la lignée perdue avec `/data` (GAP-8-01) est récupérée par le miroir de provenance de chaque racine, pas par une annexe dans l'archive (Q49, ADR-0046).

## Q41 — `Vol.X` dans le nom d'un chapitre de série en cours

**Contexte.** Les tomes sont attribués aux chapitres des mois après leur sortie. Si le nom du fichier contient `Vol.X`, chaque attribution tardive produit un plan de renommages de masse (GAP-6-07, F7).

| Option | Avantages | Inconvénients |
|---|---|---|
| `Vol.X` dans le nom quand il est connu | Classement Kavita par tome visible dans les noms | Renommages, progression perdue avant M4, faux « autre écrivain » |
| **Tome seulement dans ComicInfo** (et l'API Kavita), nom figé à la 1re exportation | Aucun renommage ; chemin figé (EF-EXP-061) | Noms de fichiers moins parlants ; dépend du rangement Kavita par ComicInfo, vérifié par golden `plugin/parse` |

**Recommandation.** Tome dans ComicInfo seulement pour une série en cours ; une série adoptée garde ses noms existants (EF-EXP-052) ; tout renommage reste une proposition explicite.
**Statut.** Tranché (F7) ; golden `plugin/parse` à confirmer en M1a.

## Q42 — Boîte de M1a après la passe 6

**Contexte.** La passe 6 ajoute à M1a surtout du schéma et des contrats (`export_item`, `unit_coverage`, `replica`, contributeurs, langues canoniques, racines physiques, `adopt_ro`, port `ConnectedService`, autorité de la base), estimés à 20–30 h, avec des tests de propriétés.

| Option | Avantages | Inconvénients |
|---|---|---|
| **Garder la boîte de 200 h** ; ajouts absorbés par la marge de l'estimation 170–250 h ; règle des 150 % inchangée | Pas de nouvelle dérive annoncée ; les ajouts évitent des migrations après M1a | Marge réduite ; la revue de la 4e semaine devient plus probante |
| Porter la boîte à 230 h | Estimation plus honnête | Premier usage réel encore repoussé |
| Reporter ces schémas en M1b ou M2 | M1a plus court | Chaque report devient une migration de données ou touche une garantie intouchable (passe 6) |

**Recommandation.** 200 h conservées ; aucun ajout de la passe 6 ne devient coupable ; si la revue de la 4e semaine projette plus de 300 h, couper les seuls coupables (ADR-0035) puis revoir Q34 et Q36.
**Statut.** Ouvert : à confirmer en **M0** avec Q34 et Q36.

## Q43 — Chinois sans écriture et étiquettes `all` / `multi`

**Contexte.** Les sources donnent `zh` sans écriture, ou `all`/`multi` au niveau d'une extension multilingue de Mihon. Ces valeurs ne peuvent pas entrer dans la clé d'Édition (ENF-I18N-005, U2).

| Option | Avantages | Inconvénients |
|---|---|---|
| **Écriture obligatoire (`zh-Hans`/`zh-Hant`) ; `all`/`multi`/`und` → langue du chapitre si connue, sinon `waiting_state = language_unknown`** | Pas d'Édition fourre-tout ; simplifié et traditionnel séparés | Une action humaine pour les sources muettes |
| Déduire l'écriture du texte ou de la région (`zh-hk` → `zh-Hant`) | Moins d'attente | Heuristique fausse sur certains contenus ; décision silencieuse dans une clé d'identité |
| Accepter `zh` nu | Simple | Deux publics mêlés dans une même Édition, fusion impossible ensuite |

**Recommandation.** Écriture obligatoire ; la correspondance par alias CLDR (`zh-hk` → `zh-Hant-HK`) est appliquée quand elle est univoque, sinon `language_unknown` avec la commande `shelfhold edition set-lang`.
**Statut.** Tranché sur le principe (F10) ; table d'alias à figer en M1a à partir du corpus relevé en S5.

## Q44 — Boîte de M1a après la passe 7

**Contexte.** La passe 7 ajoute à M1a 7 exigences de schéma et de contrat (empreintes `content_hash`/`file_hash`, identité des séries exportées, Équipe par Édition, époque de restauration, demande d'une plage, normaliseurs versionnés, tables de lecture), estimées 12 à 20 h, après les 20 à 30 h de la passe 6. M1a compte 110 exigences ; EF-INT-026 devient intouchable.

| Option | Avantages | Inconvénients |
|---|---|---|
| **Garder la boîte de 200 h et le seuil de 300 h** ; ajouts absorbés par la marge haute (250 h) ; aucun nouveau coupable | Aucune dérive annoncée ; les ajouts évitent des migrations de sens après M1a | Marge des passes 6 et 7 presque consommée ; la revue de la 4e semaine devient décisive |
| Porter la boîte à 230 h (seuil 345 h) | Estimation plus honnête | Premier usage réel encore repoussé |
| Reporter les tables de lecture (`reading_position`, bornes) en M4 | M1a un peu plus court | Les bornes de couverture se perdent pour les tomes adoptés en M1a ; il faudrait relire tous les fichiers |

**Recommandation.** 200 h conservées ; seules les colonnes et tables sont en M1a (pas de code de sync) ; si la revue de la 4e semaine projette plus de 300 h, couper les seuls coupables (ADR-0035), puis revoir Q34, Q36 et Q42 ensemble.
**Statut.** Ouvert : à confirmer en **M0** avec Q34, Q36 et Q42.

## Q45 — Contenu de `/store` : original reçu ou rendu réversible

**Contexte.** `/store` devait garder le fichier « tel que reçu », mais la mise à l'abri partait du fichier de `/library`, rendu pour Kavita (GAP-7-01). Le chasseur proposait deux règles possibles.

| Option | Avantages | Inconvénients |
|---|---|---|
| **Toujours l'original reçu ; à défaut de source, original extrait du rendu et vérifié par `content_hash`** (G1) | Fixité vérifiable, provenance prouvable (BagIt M4), aucun rendu propagé | Extraction à écrire par format ; un écart laisse l'Unité sans copie conservée |
| Accepter le rendu dans `/store` avec une procédure inverse déclarée | Mise à l'abri toujours possible | Deux natures de fichiers dans `/store`, fixité à deux vitesses, risque de propager le rendu |

**Recommandation.** Première option ; vecteurs de test d'extraction par format (CBZ avec ComicInfo, EPUB à OPF réécrit).
**Statut.** Tranché (G1) ; ADR-0043, EF-BIB-048.
**Passe 9 (J1).** Pour un EPUB `untrusted`, le fichier rangé est le dérivé neutralisé : une copie récupérée depuis la destination porte `store_provenance = extracted_derived` (vérifiée par `derived_hash`), jamais `extracted` (Q53, EF-BIB-051).

## Q46 — Roman web traduit par des équipes : Édition ou Équipe

**Contexte.** La passe 6 donnait deux Éditions pour deux traducteurs dès que le `kind` est un roman. Les romans web traduits par des équipes de fans qui se relaient (cas central de lncrawl) se seraient coupés à chaque relais (GAP-7-06).

| Option | Avantages | Inconvénients |
|---|---|---|
| **Discriminant selon la provenance déclarée par la source (`translation_nature`)** : publiée → `text_version`, communautaire → Équipe | Une seule Édition par roman web ; règle identique au manga ; traductions publiées toujours distinguées | Les sources doivent le déclarer (défaut `unknown` → règle du `kind`, journalisée) |
| Garder la règle par `kind` | Simple | Séries coupées, trous partout dans `backfill --gaps`, collisions dans `export_series` |
| Demander à l'admin à chaque relais | Exact | Charge humaine permanente |

**Recommandation.** Première option ; lncrawl déclare `community`, les catalogues `published`.
**Statut.** Tranché (G6) ; ADR-0043, EF-BIB-049.

## Q47 — Fusion des positions de lecture hors ligne

**Contexte.** Le LWW sur l'horodatage serveur (ENF-TIME-001) protège des horloges fausses, mais une liseuse ou une PWA qui se resynchronise envoie une position ancienne **après** une lecture plus récente sur un autre appareil, et cette position gagne (V2).

| Option | Avantages | Inconvénients |
|---|---|---|
| LWW sur l'horodatage serveur (situation passe 6) | Robuste aux horloges | Faux pour la lecture hors ligne |
| **`client_ts` le plus récent, borné par l'horodatage serveur (futur rejeté), historique conservé, conflit proposé** | Juste dans le cas hors ligne ; rien n'est perdu | Une question à l'utilisateur en cas de conflit |
| « Le plus loin gagne » | Simple | Faux après une relecture volontaire en arrière |

**Recommandation.** Deuxième option en M5 (EF-LEC-007) ; le schéma (`client_ts`, historique) est déjà en place dès M1a (EF-LEC-014).
**Statut.** Tranché sur le principe (G10) ; seuil de conflit à fixer en M5.

## Q48 — Kavita et Komga indexant le même arbre (ADR avant M4)

**Contexte.** Un cas homelab courant : Kavita et Komga lisent le même dossier. Avec les contrats écrits, ce seraient deux destinations `tree` sur les mêmes chemins : chacune verrait les fichiers de l'autre comme ceux d'un « autre écrivain », et chacune voudrait son propre rendu (ComicInfo, nom) (GAP-8-10). La passe 8 refuse deux liaisons `tree` qui se recouvrent (`target.tree_overlap`, EF-EXP-058) en attendant une décision.

| Option | Avantages | Inconvénients |
|---|---|---|
| **Séparer « arbre » et « indexeur »** : un arbre (sentinelle, racines, manifeste, miroir de provenance, rendu unique) auquel s'attachent un ou plusieurs indexeurs (scan, API, progression, audit d'accès) | Un seul écrivain, un seul rendu ; colle au cas réel | Le rendu doit convenir aux deux serveurs (Kavita regroupe par nom, Komga par dossier, GAP-8-09) ; concept nouveau dans le contrat |
| Deux arbres distincts, un par serveur | Aucun concept nouveau | Double espace disque ; deux rendus ; la famille voit deux bibliothèques |
| Komga en destination secondaire sans écriture, simple lecture de l'arbre Kavita | Rien à écrire pour Komga | Pas de scan ciblé ni de préservation de progression pour Komga |

**Recommandation.** Première option, à écrire comme ADR **avant M4** ; d'ici là, refus des recouvrements.
**Statut.** Ouvert : ADR avant **M4** (H3).

## Q49 — Provenance des fichiers exportés : annexe dans l'archive ou miroir à côté

**Contexte.** Sans identité dans les fichiers (Q40), la lignée d'un fichier rangé (Variante, identifiant distant, équipe) disparaît avec `/data` : une restauration en retard ou une réinstallation réadopte tout en `adopted` (GAP-8-01). Les fichiers sont figés : ce qui n'est pas décidé avant M1a ne s'appliquera jamais aux fichiers déjà rangés.

| Option | Avantages | Inconvénients |
|---|---|---|
| Annexe `shelfhold.json` dans chaque CBZ ou EPUB (proposition du chasseur) | Voyage avec chaque fichier, même déplacé à la main | Trace de l'outil et références de source dans chaque fichier, y compris hors du foyer ; annexe figée ; deux rendus selon l'egress |
| **Miroir du manifeste par racine physique** (`.shelfhold/manifest/`, clé chemin + `file_hash`, format versionné, sous intention) | Rien dans les fichiers ; voyage avec le disque ; régénérable depuis la base | Un fichier sorti de sa racine perd son entrée (retour à l'appariement par hash) |
| Manifeste dans `/data` seulement | Aucun travail | Lignée perdue avec `/data` |

**Recommandation.** Miroir par racine (ADR-0046, EF-EXP-063) ; ComicInfo `Translator` = Équipe, `Web` vide par défaut.
**Statut.** Tranché (H1).

## Q50 — Nommage d'un segment de numérotation non vide

**Contexte.** Quand la numérotation repart à 1 dans une même Édition (saisons d'un webtoon), la place porte un `number_segment` (EF-BIB-050). Il faut que Kavita range « S2 Ch.1 » à part de « S1 Ch.1 », sans marqueur que son analyseur lirait comme un tome (les formes `S1`, `{S1 END}` en sont, EF-EXP-014, Q41).

| Option | Avantages | Inconvénients |
|---|---|---|
| Segment porté par le dossier de série ou `LocalizedSeries` (une série Kavita par saison) | Aucun risque de faux tome | La saison devient une série à part dans Kavita |
| Segment porté par un track (numérotation continue `<segment>.<numéro>` en ComicInfo `Number`, nom sans marqueur) | Une seule série Kavita | Numéros affichés moins naturels ; à vérifier par golden |
| Segment rendu `Vol.N` (exception à EF-EXP-061) | Lisible | Contredit le chemin figé et Q41 ; tomes réels impossibles ensuite |

**Recommandation.** Trancher par le golden `plugin/parse` de M1a (`test_segment_naming_golden_plugin_parse`) entre les deux premières options ; tant que ce n'est pas fait, une série à segment non vide reste en `numbering_conflict`, sans export.
**Statut.** Ouvert : golden en **M1a** (H2).

## Q51 — Boîte de M1a après la passe 8

**Contexte.** La passe 8 ajoute à M1a 2 exigences (EF-BIB-050, EF-EXP-063) et des volets de schéma (enveloppe des secrets, `remote_library`, `follow_suppressed`, recouvrement d'arbres), estimés 10 à 16 h, après 12 à 20 h en passe 7 et 20 à 30 h en passe 6. M1a compte 112 exigences.

| Option | Avantages | Inconvénients |
|---|---|---|
| **Garder 200 h (seuil 300 h)**, ajouts absorbés par la marge haute, aucun nouveau coupable | Le miroir et la règle des Variantes évitent une perte définitive de lignée et des identités confondues | Marge haute (250 h) presque entièrement consommée par les passes 6 à 8 |
| Porter la boîte à 230 h (seuil 345 h) | Estimation plus honnête | Premier usage réel repoussé |
| Reporter le miroir de provenance en M1b | M1a plus court | Les fichiers rangés en M1a resteraient sans lignée récupérable |

**Recommandation.** 200 h conservées ; à la revue de la 4e semaine, si la projection dépasse 300 h, couper les seuls coupables puis revoir Q34, Q36, Q42, Q44 et Q51 ensemble ; le miroir n'est pas coupable (format sur disque).
**Statut.** Ouvert : à confirmer en **M0** avec Q34, Q36, Q42 et Q44.

## Q52 — Opérations fichier des destinations : mandataire RPC ou plan sérialisable

**Contexte.** Les destinations reçoivent un objet `f` d'opérations fichier (EF-EXP-001), objet vivant que le bac à sable JSON-RPC de M6 ne peut pas transporter tel quel (GAP-8-12, V4).

| Option | Avantages | Inconvénients |
|---|---|---|
| Déclarer `FsOps` second mandataire RPC autorisé (chemins relatifs, arguments JSON) | Peu de changement pour les auteurs | Un objet vivant de plus à travers la frontière ; appels fins, plus difficiles à auditer |
| **Plan d'opérations sérialisable** (`list[FsOp]`) rendu par la destination, validé et exécuté par l'hôte | Pur, testable par aller-retour JSON, cohérent avec `ApiCall` ; l'hôte garde `fsops` | Une destination ne peut pas lire le résultat d'une opération au milieu du plan |

**Recommandation.** Plan sérialisable, forme fixée avant la documentation des auteurs (M3), testée dans le kit destinations (M4).
**Statut.** Tranché (H3).

## Q53 — Contenu exporté : où neutraliser le XHTML d'un EPUB récupéré sur le web

**Contexte.** Le lecteur de livres de Kavita injecte le XHTML d'un EPUB dans sa page sans assainissement, garde ses jetons de session dans `localStorage` et ne pose pas de CSP (vérifié dans son code, explorateur passe 9, X1 ; précédent CVE-2024-39307). Un EPUB récupéré sur le web par un service tiers (lncrawl, flux OPDS de tiers, import) est donc un vecteur d'injection vers tous les comptes du foyer. ENF-SEC-015 ne couvrait que le texte des métadonnées. La question touche l'identité des fichiers (ADR-0043) et ce que garde `/store` (Q45).

| Option | Avantages | Inconvénients |
|---|---|---|
| Ne rien faire : la destination se défend | Aucun code ; octets reçus intacts | La destination ne se défend pas aujourd'hui ; Shelfhold relaie l'injection |
| Rendu assaini par destination, au moment de l'export | L'original reste le seul objet stocké | En mode passage, la copie de la destination est la seule qui survit ; la « récupération de l'original » depuis le rendu devient fausse ; un rendu par destination à tester |
| **Dérivé canonique neutralisé, unique, produit à l'ingestion** (`xhtml-neutralizer@vN`) pour les sources `untrusted` | Un seul fichier assaini, déterministe ; `content_hash` = original, `derived_hash` = dérivé ; récupération honnête (`extracted_derived`) | Mise en forme dynamique perdue ; une fonction versionnée de plus |

**Recommandation.** Troisième option (ADR-0047, EF-BIB-051) : `content_trust` au manifeste dès M1a (défaut `untrusted`), liste blanche des entrées CBZ dès M1a, neutraliseur livré **avant** le premier adaptateur `untrusted` de M2 ; fichiers `publisher` et tatoués intouchés hors de l'OPF.
**Statut.** Tranché (J1).

## Q54 — Effacement immédiat contre les règles de conservation

**Contexte.** Le modèle est fait pour ne rien perdre : corbeille de 30 jours, suppression définitive seulement quand aucune sauvegarde retenue ne cite la copie, « Mettre à l'abri » automatique, suivi et « Combler les trous » qui ramènent une Unité, restauration (ADR-0044). Un foyer peut pourtant devoir faire disparaître tout de suite un contenu reçu d'une source tierce (contenu dont la détention est illicite, injonction, notification, erreur grave) ; EF-BIB-021 l'interdisait (X2).

| Option | Avantages | Inconvénients |
|---|---|---|
| Suppression ordinaire puis vidage manuel de la corbeille | Rien de nouveau | Copies de `/store`, staging, miroir et Suwayomi oubliées ; réacquisition par le suivi ; réapparition après restauration |
| Purge immédiate, pierre tombale en base seulement | Simple | Une restauration de `/data` antérieure efface la pierre tombale et le contenu revient |
| **Purge immédiate, pierre tombale en base et journal hors base par racine, rejoué après restauration** | Survit à une restauration de `/data` ; bloque réacquisition, adoption et import | Un format sur disque de plus ; journal effaçable à la main |

**Recommandation.** Troisième option (ADR-0048, EF-BIB-052) : `shelfhold purge --now`, admin, confirmation tapée, audité ; aucun titre gardé ; les sauvegardes externes de l'hôte restent hors de portée et sont documentées comme telles.
**Statut.** Tranché (J2). Ouvert : levée d'une pierre tombale (`shelfhold tombstone lift`), forme à fixer en **M3** avec l'action de l'UI.

## Q55 — Faille de contrôle d'accès dans Kavita : avertir ou bloquer les étagères famille

**Contexte.** La barrière parentale principale est la bibliothèque Kavita dédiée (ADR-0027). Les avis de sécurité de Kavita du 2026-09-05 (versions ≤ 0.9.0.2, corrigées en 0.9.1) décrivent un téléchargement de pages et une lecture d'annotations sans contrôle d'accès par bibliothèque ni restriction d'âge (U1). ENF-COMP-001 ne faisait qu'avertir pour une version connue comme mauvaise.

| Option | Avantages | Inconvénients |
|---|---|---|
| Avertir seulement, comme pour les autres versions connues comme mauvaises | Aucune friction | La barrière parentale est contournable pendant que l'étagère est active |
| **Catégorie `access_control` dans `known_bad_versions` : blocage des étagères < 18+**, minimum 0.9.1 pour elles ; 0.8.9 reste le minimum des étagères adultes (avant la passe 12, Q68) | Aucun enfant exposé par une faille connue ; les étagères adultes continuent | Le foyer doit mettre Kavita à jour avant d'ouvrir une étagère famille |
| Relever le minimum global à 0.9.1 | Règle unique | Bloque aussi les foyers sans enfant |

**Recommandation.** Deuxième option (ENF-COMP-001, EF-EXP-055) ; la liste est une donnée mise à jour à chaque avis.
**Statut.** Tranché (J3).

**Passe 10 (GAP-10-04) : re-tranché par K4.** La deuxième option laissait passer le contenu **adulte** : sur une version `access_control`, tout compte non administrateur atteint toutes les bibliothèques, donc un compte enfant de la même instance lit l'étagère 18+. Options ajoutées : (d) **si l'audit trouve au moins un compte restreint, toutes les étagères liées à l'instance (18+ et X compris) sont mises en pause avec `target.child_exposure` ; sinon avertissement seulement** ; (e) refuser toute version `access_control`, même dans un foyer sans enfant (friction inutile). **Recommandation** : (d), dès M1a (EF-EXP-055, I43). **Statut** : Tranché (K4), remplace la deuxième option.

**Passe 12 (AA1) : minimum relevé.** La troisième option (« minimum global 0.9.1 ») est finalement retenue pour une autre raison : `plugin/parse`, dont M1a dépend pour le contrôle de collision, n'existe pas avant Kavita 0.9.0. Le minimum unique 0.9.1 s'applique à toutes les étagères ; 0.8.9 n'est plus le minimum des étagères adultes (Q68, M9). La règle (d) de K4 reste en vigueur pour les versions `access_control` ultérieures.

## Q56 — Boîte de M1a après la passe 9

**Contexte.** La passe 9 ajoute à M1a 2 exigences (EF-BIB-051 pour son volet contrat et liste blanche CBZ, EF-BIB-052 pour son volet M1a) et 9 volets de schéma ou de règle (miroir 1.1, compteur de génération, `fs_intent.epoch`, dérive amont, `number_segment_ordinal`, `remote_library` par serveur, trousseau, catégorie de `known_bad_versions`, décodeurs), estimés 12 à 18 h ; le neutraliseur (8 à 12 h) est compté en M2. M1a compte 114 exigences.

| Option | Avantages | Inconvénients |
|---|---|---|
| **Garder 200 h (seuil 300 h)**, ajouts absorbés, aucun nouveau coupable | Contrats et formats sur disque posés avant les premiers fichiers | Marge haute dépassée sur le papier par le cumul des passes 6 à 9 |
| Porter la boîte à 230 h (seuil 345 h) | Estimation plus honnête | Premier usage réel repoussé |
| Reporter `purge --now` en M1b | M1a plus court | Pendant M1a, un contenu à retirer ne pourrait être effacé que fichier par fichier, et reviendrait par le suivi |

**Recommandation.** 200 h conservées ; revue de la 4e semaine commune à Q34, Q36, Q42, Q44, Q51 et Q56 ; ni `purge --now` ni le champ `content_trust` ne sont coupables (contrat et exposition).
**Statut.** Ouvert : à confirmer en **M0** avec Q34, Q36, Q42, Q44 et Q51.

## Q57 — Entrée SVG ou HTML dans un CBZ : refuser ou rastériser

**Contexte.** Un CBZ venu d'une extension tierce peut contenir une entrée SVG, HTML ou un fichier dont l'extension ment sur son type (X1, U4). Le lecteur de la destination peut les afficher. La rastérisation demande un moteur de rendu SVG, absent de M1a.

| Option | Avantages | Inconvénients |
|---|---|---|
| Laisser passer et compter sur la destination | Aucun code | Même exposition que Q53 |
| **Refuser en M1a** (`content.untrusted_entry`, Unité non exportée, admin averti), **rastériser les SVG à partir de M2** | Aucune exposition dès M1a ; pas de dépendance de rendu en M1a | Une Unité légitime à SVG attend M2 |
| Retirer silencieusement l'entrée | Unité exportée | Page manquante sans le dire |

**Recommandation.** Deuxième option (EF-BIB-051 (6), ENF-SEC-004) ; extension et octets magiques doivent concorder.
**Statut.** Tranché (J1).

## Q58 — Boîte de M1a après la passe 10

**Contexte.** La passe 10 ajoute à M1a 5 exigences (EF-BIB-053, EF-SRC-036, EF-USR-022, EF-INT-027 et ENF-SEC-016 pour leur volet M1a) et 3 volets (pause de toutes les étagères d'une instance `access_control`, colonne `performance`, aucun titre dans les événements et journaux), estimés 10 à 16 h. M1a compte 119 exigences.

| Option | Avantages | Inconvénients |
|---|---|---|
| **Garder 200 h (seuil 300 h)**, ajouts absorbés, aucun nouveau coupable | Formats sur disque (journal authentifié, chiffré depuis la passe 11 ; identités amont) et sécurité posés avant les premiers fichiers | Marge haute dépassée sur le papier par le cumul des passes 6 à 10 |
| Porter la boîte à 230 h (seuil 345 h) | Estimation plus honnête | Premier usage réel repoussé |
| Reporter la signature du journal et les sessions en M1b | M1a plus court | Journal 1.0 non authentifié déjà écrit sur les racines (migration) ; session admin longue pendant M1a |

**Recommandation.** 200 h conservées ; revue de la 4e semaine commune à Q34, Q36, Q42, Q44, Q51, Q56 et Q58 ; aucune des additions n'est coupable (sécurité, contrôle parental, pierres tombales).
**Statut.** Ouvert : à confirmer en **M0** avec Q34, Q36, Q42, Q44, Q51 et Q56.

## Q59 — Identité d'un chapitre relayé par Suwayomi

**Contexte.** Suwayomi expose un identifiant entier local par chapitre (route de téléchargement `/api/v1/chapter/{id}`), renuméroté à la réinstallation ou à la restauration d'une sauvegarde. Si `remote_unit_id` le reprend, I32 le fige : Variantes en double, pierres tombales contournées, collisions entre instances (GAP-10-03).

| Option | Avantages | Inconvénients |
|---|---|---|
| Identifiant entier local | Direct, utilisé par la route | Cassé par toute réinstallation ; non migrable une fois figé |
| **`sourceId` + `chapter.url` (comme Mihon), entier local en `fetch_handle` ré-résolu** | Stable à la réinstallation et entre instances ; même identité que Mihon | Une résolution de plus après une réinstallation ; une source qui change ses URL crée une nouvelle Variante |
| URL du chapitre seule | Simple | Collision entre deux extensions qui servent les mêmes chemins |

**Recommandation.** Deuxième option, et règle du contrat : toute identité rendue par un service connecté survit à sa réinstallation, vérifiée par une réinstallation simulée au kit de conformité (EF-SRC-036, I42).
**Statut.** Tranché (K3).

## Q60 — Pierre tombale : portée et confiance du journal

**Contexte.** La pierre tombale de la passe 9 ne vise que l'Unité ; la série amont reste suivie dans Suwayomi et ramène l'Œuvre purgée (GAP-10-01). Le journal hors base, rejoué après restauration, peut déclencher des suppressions alors que le dossier partagé est écrit par d'autres (GAP-10-02).

| Option | Avantages | Inconvénients |
|---|---|---|
| Pierre par Unité, journal en clair (passe 9) | Déjà décrit | Œuvre recréée au cycle suivant ; suppression forgeable |
| **Portées `variant`, `place`, `series_binding`, `work` ; journal signé (HMAC) ; ligne non vérifiée = blocage seul ; suppression au rejeu post-restauration après confirmation de l'admin** | Rien ne revient ; rien ne s'efface sur une ligne forgée | Format 1.1 du journal ; une clé perdue réduit les lignes au blocage |
| Jamais de suppression au rejeu, blocage seul | Aucun risque de suppression | Un contenu purgé puis restauré par l'hôte reste présent jusqu'à une action manuelle |

**Recommandation.** Deuxième option (EF-BIB-053, I40, I41) ; en M1a, Shelfhold ignore la série et demande à l'admin de la retirer dans Suwayomi ; `unfollow` en M1b.
**Statut.** Tranché (K1, K2).

**Passe 11 (GAP-11-01, GAP-11-02) : volet « confiance du journal » re-tranché par L1.** Les empreintes salées par un `instance_id` public se renversent par dictionnaire (slugs de titres dans les URL) et ne survivent pas à une migration de normaliseur ; le HMAC protégeait l'intégrité, pas la confidentialité. Les portées restent ; la table et le journal stockent désormais les **valeurs brutes chiffrées en AEAD** (Q64, EF-BIB-054, I46) ; une ligne indéchiffrable est **inerte** (elle ne bloque plus) au lieu de « blocage seul ». **Statut** : Tranché (L1), remplace le journal signé de K2.

## Q61 — Sessions et ré-authentification : durées et actions concernées

**Contexte.** Une session admin oubliée sur un appareil partagé suffit à franchir la barrière parentale (classer = approuver) sans faille logicielle (Y1). Les valeurs par défaut doivent rester supportables pour l'admin d'un foyer.

| Option | Avantages | Inconvénients |
|---|---|---|
| Session longue, révocable par la CLI (actuel) | Confort | Barrière franchissable par une session oubliée |
| **Admin : 30 min d'inactivité, 12 h absolues, pas de « se souvenir » ; `fresh_auth_s` = 300 s pour classer, étagère famille, démasquer, clés et secrets, profils** | Seuls les gestes sensibles redemandent le mot de passe | Un mot de passe à retaper avant un lot de classements |
| Fraîcheur exigée pour toute écriture | Plus strict | Friction sur chaque action, contournement probable (mot de passe enregistré) |

**Recommandation.** Deuxième option (EF-USR-022, ADR-0049) ; valeurs réglables dans la référence de configuration ; type de problème `reauth-required` défini dans l'OpenAPI v1.
**Statut.** Tranché sur le principe (K5) ; valeurs par défaut à confirmer en **M1b** sur l'usage réel de l'écran « À classer ».

## Q62 — EPUB `untrusted` en mode passage : garder l'original ?

**Contexte.** En mode passage, seul le dérivé neutralisé était rangé ; un contournement découvert dans le neutraliseur ne pourrait se corriger qu'en re-neutralisant un fichier déjà réécrit, et la grille affirmait à tort « dérivé recalculable depuis l'original conservé » (GAP-10-07).

| Option | Avantages | Inconvénients |
|---|---|---|
| Dérivé seul (passe 9) | Mode passage pur | Correction de sécurité appliquée sur un dérivé ; perte de fidélité |
| **Original `untrusted` gardé dans `/store` même en mode passage** ; sans `/store`, avertissement et correction depuis le dérivé (`derived_chain`) | Correction propre depuis l'original ; EPUB petits | Exception au mode passage ; `/store` recommandé pour lncrawl |
| Re-télécharger l'original à chaque mise à jour du neutraliseur | Aucun stockage | Source disparue ou changée entre-temps |

**Recommandation.** Deuxième option (EF-BIB-051, ADR-0028, ADR-0047, I45).
**Statut.** Tranché (K9).

## Q63 — Reprise en main quand le seul admin est indisponible

**Contexte.** Si l'unique admin du foyer est indisponible, un autre adulte non technicien n'a ni le shell de l'hôte ni la phrase de passe (Y3). La lecture continue dans Kavita.

| Option | Avantages | Inconvénients |
|---|---|---|
| CLI seule (actuel) | Frontière de confiance claire (l'hôte) | Inaccessible à un non-technicien |
| **Kit de secours imprimable (sans secret) + second admin facultatif proposé par l'assistant** | Reprise possible sans affaiblir la barrière | Le kit doit être rangé hors de portée des enfants |
| Codes de récupération web | Reprise sans shell | Un enfant qui trouve le code contourne la barrière parentale |

**Recommandation.** Deuxième option (EF-USR-023, ADR-0049).
**Statut.** Tranché (K7) ; livré en **M3** avec l'assistant.

## Q64 — Pierres tombales : sel public, empreintes à clé ou valeurs chiffrées

**Contexte.** Les pierres tombales de la passe 10 stockent des empreintes salées par l'`instance_id`, écrit en clair dans `.shelfhold-mount-id`, et un `content_hash` en clair ; le journal vit sur le disque de la bibliothèque, lisible par le conteneur Kavita, les partages SMB et les sauvegardes de l'hôte. Une attaque par dictionnaire (slugs de titres dans les URL de chapitre) ou par confirmation (« ce fichier a-t-il été purgé ? ») retrouve ce que la purge devait faire oublier (GAP-11-02). Et comme les empreintes portent sur des valeurs **normalisées**, la première migration d'un normaliseur (`content_hash@v2`, alias CLDR, `migrate_ref`) les rend aveugles, sans préimage pour les recalculer (GAP-11-01).

| Option | Avantages | Inconvénients |
|---|---|---|
| Sel public et HMAC d'intégrité (passe 10) | Déjà décrit ; aucun secret requis pour bloquer | Réversible par dictionnaire ; aveugle aux migrations d'identité |
| Empreintes à clé `HMAC(K_fp, …)` avec la version de chaque normaliseur par composante (proposition du chasseur) | Non réversible sans la clé | Calcul sous chaque version citée à partir de formes canoniques versionnées ; deux clés à gérer ; toujours aveugle à une composante non prévue |
| **Valeurs brutes chiffrées en AEAD (clé dérivée du trousseau, `kid`, données associées portée + racine), re-normalisées au test d'appartenance avec le normaliseur courant et chaque version citée** | Rien de lisible sans le trousseau ; insensible aux migrations (on re-normalise à volonté) ; l'AEAD donne aussi l'intégrité ; une seule clé | Trousseau perdu ⇒ journal inerte ; index en mémoire à reconstruire au démarrage |

**Recommandation.** Troisième option (EF-BIB-054, ADR-0048, I46). **Risque accepté** : sans le trousseau, le journal ne bloque plus rien et ne supprime jamais rien ; un contenu purgé pourrait être réacquis après un sinistre total sans export. Parade : `secrets export` rappelé après la première pierre tombale et après chaque rotation, kit de secours du foyer qui **référence** l'export chiffré du trousseau sans contenir de clé (EF-USR-023, passe 12, M8), avertissement dans `shelfhold status` (R62). Passe 12 (M1) : un `kid` cité n'est jamais retiré, la rotation ne réécrit jamais le journal, et une ligne au `kid` inconnu bloque jusqu'au réimport du trousseau ou à la dérogation « trousseau perdu » (Q69). Responsable : **propriétaire** (exporter et ranger le trousseau).
**Statut.** Tranché (L1).

## Q65 — Clés d'API et actions irréversibles

**Contexte.** `fresh_auth_s` n'existe que pour les sessions ; une clé d'API `admin` n'a ni `auth_time` ni expiration obligatoire, alors que la route de `purge --now` existe dès M1a. Une clé laissée dans un script domotique suffit à effacer la bibliothèque et à en bloquer la reconstitution (GAP-11-03).

| Option | Avantages | Inconvénients |
|---|---|---|
| Table de vérité par rôle seulement (passe 10) | Simple | Une clé `admin` fait tout, sans fraîcheur |
| Portée `destructive` séparée, à durée de vie obligatoire | Automatisation possible des purges | Une portée de plus à expliquer ; la clé reste un secret longue durée |
| **Axe crédential : actions irréversibles, secrets et contrôle parental refusés à toute clé et à tout jeton ; session fraîche ou CLI locale seulement ; clés `admin` à expiration obligatoire (≤ 90 j)** | Aucune clé volée ne détruit ni n'ouvre la barrière ; règle uniforme | Une purge scriptée passe par la CLI de l'hôte |

**Recommandation.** Troisième option (EF-USR-024, ADR-0049, I47) ; la portée `destructive` reste réservée. **Amendé par l'arbitrage porteur P4** : l'expiration des clés `admin` devient facultative ; le refus aux clés et jetons est maintenu (Q72).
**Statut.** Tranché (L2).
**Complément passe 13 (N2, N5).** La règle vise les actions **déclenchées par un crédential** ; une liste fermée d'émetteurs système (staging orphelin, sonde, corbeille au-delà de 30 j, garde disque), limités aux chemins créés par l'hôte, en est exemptée ; les sorties d'EF-INT-023 sont classées ; le socket de la CLI refuse un pair hors de l'espace de noms PID du conteneur (R67).

## Q66 — Âge dans les EPUB exportés et échelle d'âge

**Contexte.** EF-EXP-033 prévoyait d'écrire le classement en `<meta property="shelfhold:age">` et en `dc:subject`. Kavita convertit **tout** `dc:subject` en genre et ne lit aucun âge dans un EPUB ; le libellé resterait figé dans le fichier (EF-EXP-040) et `shelfhold:age` est une trace de l'outil hors du foyer, au préfixe non déclaré (Z3-a). L'échelle « Tous publics · 10+ · 13+ · 16+ · 18+ · X » n'a pas le palier 12 commun à PEGI, FSK et à la signalétique française (Z3-b).

| Option | Avantages | Inconvénients |
|---|---|---|
| Écrire `shelfhold:age` et `dc:subject` (passe 2) | Âge lisible dans le fichier | Genre parasite dans Kavita, libellé figé, trace de l'outil, OPF non conforme |
| **Aucun âge écrit ; lu seulement comme suggestion à l'adoption ; âge stocké en entier minimal + drapeau X, libellés i18n, palier 12 → `Teen`** | Barrière inchangée (bibliothèque) ; paliers ajoutables sans migration | L'âge n'est visible que dans Shelfhold et le miroir de provenance |
| Écrire un libellé traduit selon la locale | Lisible par l'utilisateur | Mêmes défauts, plus un fichier différent par langue |

**Recommandation.** Deuxième option (EF-EXP-033, EF-EXP-011, ADR-0027, I49) ; à corriger avant M2 (EPUB passthrough), schéma de l'âge dès M1a.
**Statut.** Tranché (L5).

**Passe 12 (M6, GAP-12-06).** Sans âge dans le fichier, Kavita classait la série EPUB « inconnue » et la masquait aux comptes enfants restreints (sans « inclure les inconnus », exigé par l'audit). L'âge de série est donc **poussé verrouillé par l'API Kavita** dès que l'Œuvre est classée et à chaque reclassement (EF-EXP-040, M2) ; toujours rien dans les fichiers.

## Q67 — Boîte de M1a après la passe 11

**Contexte.** La passe 11 ajoute à M1a 2 exigences (EF-BIB-054, EF-USR-024), avance ENF-SEC-010 (axe crédential) de M1b à M1a et ajoute des volets de schéma ou de contrat (âge entier minimal, `apprise` épinglé, `source.dns_blocked`, `lan_targets` IPv6, registre des normaliseurs cités), estimés 8 à 14 h. M1a compte 122 exigences (86 EF, 36 ENF).

| Option | Avantages | Inconvénients |
|---|---|---|
| **Garder 200 h (seuil 300 h)**, ajouts absorbés, aucun nouveau coupable | Format chiffré des pierres tombales et axe crédential posés avant la première écriture sur les racines et avant la route de purge | Marge haute dépassée sur le papier par le cumul des passes 6 à 11 |
| Porter la boîte à 230 h (seuil 345 h) | Estimation plus honnête | Premier usage réel repoussé |
| Reporter le chiffrement des pierres tombales en M1b | M1a plus court | Journal 1.1 aux empreintes salées déjà écrit, non migrable (préimages effacées) |

**Recommandation.** 200 h conservées ; revue de la 4e semaine commune à Q34, Q36, Q42, Q44, Q51, Q56, Q58 et Q67 ; aucune des additions n'est coupable (pierres tombales, autorisations).
**Statut.** Ouvert : à confirmer en **M0** avec Q34, Q36, Q42, Q44, Q51, Q56 et Q58.

## Q68 — Version minimale de Kavita : double minimum ou minimum unique

**Contexte.** ENF-COMP-001 fixait deux minimums : 0.8.9 pour les étagères adultes (clés nommées à expiration) et 0.9.1 pour les étagères famille (Q55, J3). L'explorateur de la passe 12 (AA1) a vérifié dans les sources de Kavita que `GET /api/plugin/parse` et `POST /api/Plugin/parse-bulk` n'existent qu'à partir du tag 0.9.0 : sur 0.8.9.x, le contrôle de collision au plan, la lecture des fichiers adoptés, la sonde lue par Kavita et le golden de nommage appelleraient une route qui renvoie 404. Ces versions sont aussi touchées par CVE-2026-47202 (prise de contrôle de compte, corrigée en 0.9.0.2). L'API de Kavita n'a ni préfixe de version ni politique de dépréciation (26 routes retirées entre 0.8.9.1 et 0.9.1.4, AA2), et la version minimale n'était jamais testée.

| Option | Avantages | Inconvénients |
|---|---|---|
| Garder 0.8.9 pour les étagères adultes, avec repli sans `plugin/parse` (règle d'avant la passe 12) | Foyers sur une vieille version servis | Plan sans contrôle de collision ou plan en échec ; version supportée exposée à une CVE d'authentification ; deux chemins de code |
| **Minimum unique 0.9.1 pour toutes les étagères ; 404 sur une route de la liste ⇒ mode dégradé (`degraded_feature`, `target.route_missing` depuis la passe 13 ; version hors plage : `degraded_readonly`) ; liste des routes `docs/kavita-routes.md` testée contre l'`openapi.json` figé de chaque version ; nightly {minimum, épinglée, dernière}** | Règle simple ; table de vérité allégée ; jamais de plan sans contrôle de collision ; la version minimale est enfin testée | Un foyer en 0.8.9 doit mettre Kavita à jour avant M1a (minimum relevé) |
| Suivre seulement la dernière version de Kavita | Aucune matrice | Mises à jour forcées ; casse au premier retrait de route |

**Recommandation.** Deuxième option (ENF-COMP-001, EF-EXP-009, EF-EXP-014, EF-EXP-049, ADR-0020, R5) ; `plugin/parse` présenté comme contrôle du **nom** seulement, le rangement étant vérifié contre un Kavita réel en nightly (AA3).
**Statut.** Tranché (M9), avant M1a.
**Complément passe 13 (N4).** Les deux sens de « mode dégradé » sont nommés : `degraded_readonly` (version hors plage, aperçu seulement) et `degraded_feature` (404 sur une route de la liste, `target.route_missing`, sortie `target recheck`).

## Q69 — Ligne de pierre tombale au `kid` inconnu au rejeu : bloquer ou rester inerte

**Contexte.** Depuis L1, une ligne du journal qui ne se déchiffre pas est inerte (Q64). Le chasseur de la passe 12 (GAP-12-01) montre un cas où le trousseau **n'est pas perdu** : rotation, puis purge (lignes sous le nouveau `kid`), puis restauration d'une sauvegarde de `/data` antérieure à la rotation. Le trousseau restauré ignore le nouveau `kid` : les lignes deviendraient inertes et la série encore suivie redemanderait le contenu purgé, contre I36. La grille annonçait en outre un « rechiffrement par rotation » incompatible avec un journal en ajout seul.

| Option | Avantages | Inconvénients |
|---|---|---|
| Inerte (règle de la passe 11 appliquée telle quelle) | Rien à ajouter | Viole I36 après une restauration ordinaire, alors que R62 ne couvre que la perte du trousseau |
| Rechiffrer le journal à chaque rotation (écarté en passe 12 : le journal n'est jamais réécrit) | Un seul `kid` actif | Réécrit un journal en ajout seul ; une racine démontée garde l'ancien `kid` ; détection de troncature brouillée |
| **`kid` cité jamais retiré, rotation sans réécriture ; ligne au `kid` inconnu distinguée d'un échec d'authentification : elle bloque la réadoption et la réacquisition (racine et portée en clair), `tombstone.key_unknown`, « réimporter le trousseau » ; sortie par `secrets import` ou par la dérogation tapée et auditée « trousseau perdu »** | I36 tenu tant que le trousseau existe quelque part ; la perte devient un constat explicite de l'admin | Réacquisition suspendue sur la racine jusqu'au réimport ; une commande réservée de plus |

**Recommandation.** Troisième option (ENF-SEC-005, EF-BIB-053, EF-BIB-054, ADR-0048, ADR-0044, R62).
**Statut.** Tranché (M1), avant M1a.
**Complément passe 13 (N1).** Le blocage ne vaut que pour un `kid` inscrit au **registre des `kid` émis** (base et sentinelle) ; un `kid` absent du registre désigne une ligne forgée, inerte (Q70).

La passe 12 (arbitrages `M1`–`M10`, `review/ARBITRAGES-passe12.md`) est la **première passe sans manque significatif** (chasseur 0, explorateur 0) ; elle corrige des contradictions, complète Q55 (minimum relevé), Q64 (parades) et Q66 (âge de série poussé par l'API Kavita), ouvre et tranche Q68 et Q69 et ajoute aux risques résiduels les copies hors ligne non révocables (R66). La boîte de M1a ne change pas (122 exigences ; les volets ajoutés — socket de la CLI, fraîcheur minimale, lignes en attente par racine, liste des routes Kavita — sont estimés 4 à 8 h et absorbés, Q67).

## Q70 — Ligne de pierre tombale au `kid` inconnu : comment distinguer une sauvegarde ancienne d'une ligne forgée

**Contexte.** Q69 (passe 12) fait **bloquer** une ligne dont le `kid` est inconnu du trousseau, pour tenir I36 après la restauration d'une sauvegarde antérieure à une rotation. Le chasseur de la passe 13 (GAP-13-01) relève que le `kid` est en clair et choisi par celui qui écrit la ligne : un faussaire qui invente un `kid` suspend la réacquisition d'une portée entière, et la seule sortie (`accept-lost-keyring`) rend aussi inertes les lignes légitimes. Plusieurs textes disaient encore « clé inconnue ⇒ inerte ».

| Option | Avantages | Inconvénients |
|---|---|---|
| Garder la règle de la passe 12 telle quelle | Rien à ajouter | Un `kid` inventé bloque une portée ; contradiction avec EF-BIB-053 (3), le test d'EF-BIB-054 (5), la matrice de pannes et R58 |
| Revenir à « `kid` inconnu ⇒ inerte » | Aucun déni de service | Viole I36 après une restauration ordinaire (raison de Q69) |
| **Registre des `kid` émis, en base et recopié dans la sentinelle de chaque racine (ajout seul) : `kid` absent du trousseau mais présent au registre ⇒ blocage (`tombstone.key_unknown`) ; absent du registre ⇒ ligne forgée, inerte (`tombstone.unverified`, cause `kid_unregistered`)** | I36 tenu après une restauration (la sentinelle, hors de `/data`, connaît le nouveau `kid`) ; un `kid` inventé ne bloque plus ; le registre sert aussi au contrôle « jamais retiré » pour les racines démontées | Un faussaire qui écrit aussi la sentinelle bloque encore (il a déjà l'uid de Shelfhold : déni de service seulement) ; un champ de sentinelle de plus, versionné |

**Recommandation.** Troisième option (EF-BIB-053, EF-BIB-054, ENF-SEC-005, ADR-0048, ADR-0044, R58, R62).
**Statut.** Tranché (N1), avant M1a.

## Q71 — Transport `push` des destinations : corps d'`ApiCall`, sous-processus ou rien

**Contexte.** Le contrat de destination déclare `transport = push` (Send-to-Kindle, Calibre), mais aucune opération ne porte d'octets par HTTP : `ApiCall` n'a qu'un corps JSON, `FsOp.write` vise une racine de fichiers. L'explorateur de la passe 13 (AB1, AB2) a vérifié que le serveur de contenu de calibre accepte le fichier en corps brut (`/cdb/add-book`) et qu'Audiobookshelf a une route d'upload qui ne scanne pas et masque ses erreurs.

| Option | Avantages | Inconvénients |
|---|---|---|
| Opération « exécuter un programme » (`calibredb` en sous-processus) | Réutilise l'outil officiel | Embarque calibre (GPL-3.0, Qt) dans l'image ; nouvelle surface d'exécution ; aucun équivalent pour les autres cibles |
| **`ApiCall.body` facultatif (JSON ou référence de contenu, brut ou multipart) soumis aux règles de `FsOp.write` ; `push` réservé dans le kit jusqu'à M4 ; Calibre par `/cdb/*` ; Audiobookshelf jamais par l'upload** | Ajout compatible du contrat ; un seul canal de sortie du contenu, avec les mêmes garde-fous (référence planifiée, tatoués jamais vers l'extérieur, miroir jamais envoyé) | `/cdb/*` est un protocole interne de calibre : plage de versions étroite et nightly épinglé |
| Ne rien faire avant M4 | Aucun travail | Un plugin tiers peut déclarer `push` sans que le cœur sache l'exécuter |

**Recommandation.** Deuxième option (EF-EXP-001, EF-EXP-023, EF-EXP-035, ADR-0011, ADR-0024).
**Statut.** Tranché (N6), avant le kit destinations (M4).

La passe 13 (arbitrages `N1`–`N6`, `review/ARBITRAGES-passe13.md`) est la **deuxième passe consécutive sans manque significatif** (chasseur 0, explorateur 0) : **la boucle de revue est close** selon le critère d'arrêt du propriétaire (deux passes consécutives sans manque d'architecture au sens strict a/b/c). Elle corrige des contradictions de texte et de tests, ajoute des précisions additives, complète Q65 (actions réservées déclenchées par un crédential, émetteurs système exemptés, pair hors de l'espace de PID refusé), Q68 (modes `degraded_readonly`/`degraded_feature`) et Q69 (registre des `kid` émis, Q70), ouvre et tranche Q70 et Q71. La boîte de M1a ne change pas (122 exigences ; volets ajoutés estimés 3 à 6 h, absorbés, Q67). Les questions encore **ouvertes** (récapitulatif) se tranchent à leur jalon ; elles ne relancent pas la boucle.

## Q72 — Sécurité proportionnée : quelles mesures imposer à un petit logiciel auto-hébergé

**Contexte.** Les passes 10 à 13 ont empilé des mesures pensées pour un foyer exposé derrière un proxy d'authentification précis : sessions admin de 30 min, fraîcheur de 300 s sur classer, démasquer et gérer les clés, expiration obligatoire des clés `admin`, secret de proxy obligatoire, réseau dédié. Le porteur rappelle que Shelfhold est un petit logiciel auto-hébergé, que les autres utilisateurs n'auront pas son proxy, et qu'il faut **aucune faille** sans **imposer** toutes ces mesures.

| Option | Avantages | Inconvénients |
|---|---|---|
| Tout imposer (état après passe 13) | Défense maximale | Friction quotidienne ; suppose une infrastructure ; contournements probables (mot de passe enregistré) |
| **Protections invisibles toujours actives ; mesures à friction dans un profil durci facultatif ; sessions de 30 j ; ré-authentification (900 s) limitée aux actions irréversibles, aux secrets et au contrôle parental quand un profil restreint existe ; clés à expiration facultative ; forward-auth désactivé par défaut, CIDR obligatoire une fois activé, secret recommandé ; conteneur jamais bloquant** | Aucune faille ouverte par défaut ; confort ; aucun proxy supposé | Session admin oubliée sur un appareil partagé hors profil durci (R69) |
| Aucune ré-authentification | Simplicité | Une session volée purge la bibliothèque ou ouvre la barrière parentale |

**Recommandation.** Deuxième option (ENF-SEC-008, EF-USR-002, EF-USR-003, EF-USR-022, EF-USR-024, ENF-SEC-006, ADR-0018, ADR-0049, ADR-0030).
**Statut.** Tranché par le porteur (arbitrages porteur 1, P1–P6) ; remplace les valeurs de Q61 et l'expiration obligatoire de Q65.

## Q73 — Passkeys et bibliothèque d'authentification

**Contexte.** Le porteur demande les passkeys et cite better-auth comme bibliothèque appréciée. Le serveur est un monolithe Python (FastAPI) ; le front est une SPA SvelteKit statique servie par ce serveur.

| Option | Avantages | Inconvénients |
|---|---|---|
| better-auth | Complète (sessions, passkeys, OIDC, 2FA), bonne DX | TypeScript : exige un serveur Node à côté de FastAPI, deux sources de vérité des sessions et de la table de vérité |
| fastapi-users | Intégrée à FastAPI | Mode maintenance ; pas de WebAuthn |
| **Briques Python ciblées : `argon2-cffi`, sessions serveur SQLite, `webauthn` (py_webauthn), `Authlib` (M5) ; passkeys découvrables en M1b, mot de passe en repli, aucun code de récupération web** | Un seul runtime ; dépendances petites et maintenues ; passkeys résistantes à l'hameçonnage | Code de session propre (petit, déjà imposé par la table de vérité) ; passkeys indisponibles en `http://IP` |

**Recommandation.** Troisième option (EF-USR-025, EF-USR-021, ADR-0050, ADR-0018).
**Statut.** Tranché par le porteur (arbitrages porteur 1, P7, P8).

> Nommage : les arbitrages du porteur `P1`–`P8` (`review/ARBITRAGES-porteur1.md`) ne sont pas les portes de décision P1–P3 d'ADR-0038 ; le contexte « arbitrage porteur » les distingue.

## Q74 — Thèmes du front : une direction ou quatre

**Contexte.** `front-propositions.html` propose quatre directions (A Bibliothèque, B Console, C Galerie, D Calme). Q18 recommandait une seule direction pour la 1.0. Le porteur les apprécie toutes et demande A par défaut avec la possibilité de changer de thème.

| Option | Avantages | Inconvénients |
|---|---|---|
| Une seule direction | Une seule surface à tester | Écarte trois directions voulues ; pas de choix par membre |
| Quatre fronts distincts | Chaque direction sans compromis | Quatre fois les écrans ; intenable seul |
| **Socle unique, thèmes = jetons W3C + variantes d'une liste fermée ; A par défaut ; choix par utilisateur ; A en M3, B, C, D en M4** | Choix offert sans dupliquer les écrans ; contraste vérifié par thème | B et C un peu rabotés ; tests visuels par thème (R71) |

**Recommandation.** Troisième option (EF-UX-012, ADR-0051, I51).
**Statut.** Tranché par le porteur (arbitrage porteur 2, Q2).

## Q75 — Suivi automatique : rang des sources, repli, trous, cadence, filtres

**Contexte.** Le porteur demande si le suivi automatique égale celui de Suwayomi. Le socle existait (EF-SRC-009/025/026/029, EF-EXP-053 : nouveautés seulement, préférence d'équipe, relance bornée, fraîcheur), mais le suivi était porté par **une** liaison : pas de repli quand une source meurt, pas de détection automatique des trous au-delà du filigrane, cadence fixe, filtres limités à l'équipe.

| Option | Avantages | Inconvénients |
|---|---|---|
| Garder une liaison par série (migration manuelle, comme Mihon) | Simple | Une source morte arrête le suivi en silence jusqu'à l'intervention de l'admin |
| **Suivi porté par l'Édition, liaisons classées, une demande par cycle, repli notifié, détection des trous, cadence adaptative, filtres par étagère** | Au-delà de Suwayomi/Mihon (qui ne savent pas combiner deux sources pour une série) ; aucune bascule silencieuse | Repli qui mélange des traductions (R70) ; plus de tests en M2 |
| Agrégation « premier arrivé » sans rang | Nouveautés au plus tôt | Fichiers de qualité et d'équipes hétérogènes ; upgrades en cascade |

**Recommandation.** Deuxième option : grain du suivi fixé dès le schéma de M1a, comportement en M2, UI en M3 (EF-SRC-037, EF-SRC-038, EF-SRC-039, EF-BIB-056, I52).
**Statut.** Tranché par le porteur (arbitrage porteur 2, Q3).

## Risques résiduels acceptés et responsables (passe 5)

Chaque risque résiduel accepté a un **responsable** et un jalon de revue (GAP-5-06, GAP-5-11, lentille (d) du chasseur passe 5 ; lignes ajoutées par la passe 6 en fin de tableau).

| Risque résiduel | Pourquoi il est accepté | Responsable | Jalon / revue | Parade en place |
|---|---|---|---|---|
| Compte Kavita enfant exposé entre deux audits, ou compte enfant non marqué dans Kavita | Shelfhold ne modifie jamais les comptes Kavita | **Propriétaire** (rôle admin de Kavita) | M1a ; audit quotidien | Audit en lecture seule avant liaison et chaque jour, `target.child_exposure` (EF-EXP-055) |
| Contenu adulte visible avant classement dans une bibliothèque enfant hétérogène adoptée | Kavita gère l'accès existant | **Propriétaire** | M1b | Non classés en « À classer », suivi suspendu, « retenir tant que non classé » (EF-EXP-048, EF-EXP-050) |
| Exposition juridique du dépôt public avant Q27b | Un avis complet avant tout commit retarderait tout | **Mainteneur** | Q27a avant le premier commit ; Q27b avant 1.0 | Posture ADR-0015, aucun collecteur ni nom de source, procédure de notification |
| Compte Kavita dédié forcément administrateur | `scan-folder` l'exige | **Propriétaire** (`--accept-admin-risk`) | M1a | Clé nommée à expiration, liste blanche des routes, rotation (ENF-SEC-013) |
| Suwayomi en `ui_login` non pris en charge en M1a | Boîte de M1a | **Mainteneur** (suivre la stable) ; **propriétaire** (rester en `basic_auth`) | M1b | Refus clair, jamais de suivi partiel (EF-SRC-031) |
| Perte d'au plus une journée de métadonnées en M1a | Sauvegarde intégrée en M1b | **Propriétaire** | M1a → M1b | Sauvegarde quotidienne de `/data` par l'hôte, RPO 24 h, clé exportée (ENF-SEC-005) |
| Estimations de temps fausses d'un facteur 2 | Projet bénévole, pas de référence | **Mainteneur** | Revue de la 4e semaine de M1a | Vélocité, coupables écrits, Q36 |
| Signal de validation tiré d'un seul foyer | Pas de panel disponible | **Mainteneur** | Porte P2 | M3-lite garde le contrôle parental (Q37) |
| Signalement Safe Browsing du domaine de l'instance | Hors du contrôle du projet | **Propriétaire** | 1.0 | Page de connexion neutre, doc « exposition publique », démos sur un domaine distinct (ENF-PRIV-002) |
| Quota Google Books non publié, CGU Kobo, Chromium des Kindle | Information non publiée | **Mainteneur** | M3 / M5 | Budget prudent, proxy opt-in, `/lite` (`05` §10) |
| Plage de chapitres d'un tome adopté inconnue en M1a | Seuls le préfixe « Vol.N » de la source et la saisie manuelle sont exigés en M1a | **Propriétaire** (saisir le filigrane) | M1a → M2 (ComicInfo, fournisseur) | Jamais de suivi automatique : `coverage_unknown` et `shelfhold adopt set-follow-from` (EF-BIB-044, EF-EXP-053) |
| Fichier déplacé à la main hors du manifeste, sans identifiant dans le fichier | Choix de discrétion (Q40) | **Propriétaire** | M1a | Reconnaissance par hash, `reconcile --adopt-after-restore`, manifeste sauvegardé (ADR-0040, ENF-OPS-020) |
| Original d'une Unité irremplaçable impossible à extraire du fichier rendu (source disparue, rendu non réversible) | `/store` n'accepte jamais un rendu (Q45) | **Propriétaire** (garder les copies de l'hôte) | M1b (mise à l'abri) | Unité affichée « irremplaçable sans copie conservée », `content_hash` vérifié, rien d'écrit en cas d'écart (EF-BIB-048) |
| Rapport de restauration mal lu avant la réadoption | La réadoption est la seule sortie de `restored_behind` | **Propriétaire** | M1a | Rapport des écarts avant la commande, aucune suppression, corbeille et manifeste (EF-INT-026, ADR-0044) |
| Bornes de couverture inconnues pour un tome adopté | Lire chaque fichier coûterait cher en M1a | **Mainteneur** | M1a → M4 | Bornes nulles acceptées, progression par tome signalée comme telle, recalcul quand les bornes arrivent (EF-LEC-014) |
| Identité de fichier instable sur NFS ou CIFS sans `serverino` | `/library` sur partage réseau accepté (ENF-OPS-010) | **Propriétaire** | M1a | Identité (ino, taille, mtime, hash), sonde des capacités par racine, échec sûr en pause (EF-EXP-059) |
| Miroir de provenance effacé ou fichier déplacé à la main hors de sa racine (passe 8) | Rien n'est écrit dans les fichiers (Q49) | **Propriétaire** | M1a | Appariement par hash et chemin, état `adopted` ; `shelfhold meta rebuild` régénère le miroir depuis la base (EF-EXP-063, ADR-0046) |
| Marqueur de saison non reconnu par le normaliseur (passe 8) | Les formes de saison varient d'une source à l'autre | **Propriétaire** (trancher les `numbering_conflict`) ; **mainteneur** (enrichir le normaliseur) | M1a | Jamais de révision ni de remplacement : suggestion `numbering_conflict`, pas d'export avant décision (EF-BIB-050) |
| Clé maîtresse gardée dans `/data`, donc dans les copies de l'hôte (passe 8) | Défaut le plus simple au premier démarrage | **Propriétaire** | M1a | `SHELFHOLD_SECRET_KEY_FILE` hors de `/data` recommandé, avertissement dans `shelfhold status` (ENF-SEC-005) |
| Kavita et Komga sur le même arbre refusés jusqu'à l'ADR (passe 8) | Le contrat ne sait pas encore attacher deux indexeurs à un arbre | **Mainteneur** | Avant M4 | Refus `target.tree_overlap` explicite, ADR Q48 avant M4 (EF-EXP-058) |
| Contournement du neutraliseur par une construction non prévue (passe 9) | Aucune liste blanche n'est prouvée complète | **Mainteneur** | M2, puis à chaque avis | Liste blanche nh3, analyse par `safe_parse_xml()`, corpus d'EPUB piégés enrichi, version du neutraliseur dans chaque empreinte pour régénérer (EF-BIB-051, ADR-0047) |
| Copies hors de portée de `purge --now` : sauvegardes externes de l'hôte, caches et signets de Kavita, copies faites à la main (passe 9) | Shelfhold ne gère pas ces copies | **Propriétaire** | M1a | Rapport de purge qui les liste, documentation admin (EF-BIB-052, ADR-0048) |
| EPUB `untrusted` en mode passage **sans `/store` configuré** : seul le dérivé neutralisé survit (passe 9 ; restreint en passe 10, K9) | Depuis la passe 10, l'original est gardé dans `/store` même en mode passage ; le risque ne demeure que sans `/store` | **Propriétaire** (configurer `/store` pour les sources `untrusted`) | M2 | Avertissement à la liaison, correction depuis le dérivé avec `derived_chain`, « original perdu, dérivé présent » (EF-BIB-038, EF-BIB-051) |
| Étagère adulte sur une version de Kavita classée `access_control` **sans aucun compte restreint** (passe 9 ; restreint en passe 10, K4) | Dès qu'un compte restreint existe, toutes les étagères sont en pause ; sans compte restreint, aucun enfant n'est identifiable | **Propriétaire** (marquer les comptes enfants dans Kavita) | M1a | Avertissement dans `shelfhold status`, minimum 0.9.1 recommandé (ENF-COMP-001, EF-EXP-055) |
| Session adulte de **Kavita** ouverte sur un appareil partagé (passe 10) | Hors de portée de Shelfhold, invisible à l'audit | **Propriétaire** | M1b | Doc famille : un compte Kavita par lecteur, déconnexion des comptes adultes (EF-USR-022) |
| Série purgée restée dans la bibliothèque Suwayomi en M1a (passe 10) | `unfollow` n'arrive qu'en M1b (EF-SRC-029) | **Propriétaire** (retirer la série dans Suwayomi) | M1a → M1b | Série ignorée par le suivi, `series.tombstoned_upstream`, alerte (EF-BIB-053) |
| Retour arrière de tout le pool par l'hôte (instantané contenant `/data` et `/library`) qui annule une purge (passe 10) | Indétectable depuis le conteneur | **Propriétaire** | M1b (doc) | Doc « instantanés de pool et pierres tombales », export des pierres tombales hors de l'hôte (EF-BIB-053, ADR-0048) |
| Clé du trousseau perdue : lignes du journal de pierres tombales indéchiffrables (passe 10 ; **re-tranché en passe 11, L1**) | Depuis L1, **trousseau perdu ⇒ journal inerte** : il ne bloque plus rien et ne supprime jamais rien ; un contenu purgé peut être réacquis après un sinistre sans export (Q64, R62) | **Propriétaire** (exporter le trousseau dans un fichier chiffré séparé et le ranger à l'endroit indiqué par le kit de secours) | M1a ; M3 (kit) | `tombstone.unverified`, rapport, `secrets export` rappelé après la première pierre tombale et après chaque rotation, avertissement dans `shelfhold status`, kit qui **référence** l'export sans contenir de clé (passe 12, M8) ; `kid` cité jamais retiré, ligne au `kid` inconnu bloquante jusqu'à la dérogation « trousseau perdu » (passe 12, M1, Q69) (EF-BIB-053, EF-BIB-054, ENF-SEC-005, EF-USR-023) |
| Genre d'âge absent de Kavita pour les EPUB (passe 11) | Kavita convertit tout `dc:subject` en genre et ne lit aucun âge dans un EPUB ; aucun âge n'est donc écrit (Q66) | **Propriétaire** | M2 | La barrière reste la bibliothèque dédiée (ADR-0027) ; âge en base et dans le miroir de provenance ; depuis la passe 12 (M6), âge de série poussé verrouillé par l'API Kavita, donc série visible aux comptes enfants restreints (EF-EXP-040) |
| Purge scriptée impossible par clé d'API (passe 11) | Les actions réservées exigent une session fraîche ou la CLI locale (Q65) | **Propriétaire** | M1a | `shelfhold purge --now` par la CLI de l'hôte ; portée `destructive` réservée si un besoin réel apparaît (EF-USR-024) |
| Admin submergé par « À classer » pour chaque Édition nouvelle (passe 11) | Une Édition nouvelle n'entre jamais dans une étagère famille sans classement (L4) | **Propriétaire** | M2 | Classement en lot, suggestion d'âge affichée, seules les étagères < 18+ concernées (EF-BIB-055) |
| Copie hors ligne d'un contenu retiré : fichier téléchargé par OPDS ou `/lite`, ou copie d'une liseuse, conservé après une purge, un reclassement 18+ ou le retrait d'une étagère (passe 12, AA4, R66) | Un fichier sorti de Shelfhold par OPDS ou `/lite` reste hors ligne chez le lecteur et ne peut pas être repris, comme pour Kavita ; seule la PWA est sous contrôle | **Propriétaire** | M5 | Éviction du cache de la PWA à la reconnexion par `?since=` avec retraits (EF-LEC-012, EF-INT-001), jetons d'appareil révocables (EF-USR-022), doc famille (ADR-0012) |
| Processus de l'hôte sous l'uid de Shelfhold, ou conteneur qui monte `/data`, traité comme CLI locale (passe 13, GAP-13-07, R67) | Shelfhold ne contrôle pas les uid de l'hôte ; un pair hors de l'espace de PID du conteneur est déjà refusé sauf `SHELFHOLD_CLI_ALLOW_HOST=1` | **Propriétaire** (uid dédié, `/data` monté par Shelfhold seul) | M1a ; M6 (plugins) | Refus du pair hors de l'espace de PID, réglage journalisé et signalé par `shelfhold status`, doc « l'uid de Shelfhold sur l'hôte vaut un admin » (EF-USR-024, ADR-0049) |
| Lignes de pierres tombales chiffrées sous une clé divulguée, lisibles jusqu'à `tombstone rekey` (passe 13, GAP-13-06) | Il faut à la fois la fuite de la clé maîtresse, qui expose déjà tous les secrets, et l'accès au journal | **Propriétaire** (lancer `tombstone rekey` après une fuite) | M1b | `tombstone rekey` sous intention, ancien `kid` marqué compromis, doc de rotation après fuite (EF-BIB-054, ENF-SEC-005, ADR-0048) |
| Session admin oubliée sur un appareil partagé hors profil durci (R69) | Sécurité proportionnée voulue par le porteur (Q72) | **Propriétaire** (activer le profil durci ou se déconnecter) | M1a | Fraîcheur sur contrôle parental dès qu'un profil restreint existe, audit par session, passkeys, doc famille |
| Publication directe sans relecture juridique ni recherche formelle de marque (Q27a levée, porteur, 2026-11-04) | Décision du porteur : un avis préalable retarderait la publication | **Porteur** | Premier commit public ; Q27b avant 1.0 | Posture ADR-0015, aucun collecteur ni nom de source, procédure de notification ; recherche web de marque faite le 2026-11-04 |

---

## Récapitulatif

| Q | Sujet | Décision ou recommandation | Statut |
|---|---|---|---|
| Q1 | Nom | **Shelfhold** (nom anglais) ; réserver les noms dès M0 | Tranché (porteur, 2026-11-04), ADR-0019 |
| Q2 | Front | SvelteKit 3 SPA épinglé ; repli React 19 ; `/lite` en rendu serveur | Tranché (A14), spike S2 |
| Q3 | Jobs | Huey `>=3.5.4,<3.6` + couche Shelfhold (`last_run_at`, rattrapage unique) ; spike à 6 critères | Tranché (A12, B24), spike S1 |
| Q4 | Postgres | SQLite seul ; nightly Postgres retirée | Tranché (A16) |
| Q5 | Standard Ebooks | OPDS si identifiants patron, sinon une URL par livre | Ouvert, M2 |
| Q6 | Achats | Fichiers sans DRM seulement ; crochet générique ; aucun outil nommé | Tranché (A17) |
| Q7 | Lecteur en V1 | Non : secondaire, en M5 | Tranché (A1, A3) |
| Q8 | Stockage | Mode passage par défaut ; copie conservée en option dans `/store`, copie indépendante (jamais de lien dur) ; hardlink optionnel pour le contenu remplaçable ; parent monté | Tranché (A11, A20, B6, B10, D9) |
| Q9 | Sablier | Shelfhold toujours actif ; WakeHook pour Suwayomi ; vérification pilotée | Tranché (A23, C5) |
| Q10 | API tierce | Kavita sert les clients ; sous-ensemble Komga plus tard | Ouvert, Plus tard |
| Q11 | Métadonnées | Contrat + OpenLibrary en M2 ; autres en M4 ; ENF-LEG-005 | Tranché (A7) |
| Q12 | PWA hors ligne | M5 | Tranché (A3) |
| Q13 | lncrawl | Adaptateur vers l'instance de l'utilisateur ; aucun crawler activé | Tranché (A19) |
| Q14 | GitHub | Organisation gratuite | Tranché (A16) |
| Q15 | Suwayomi | CBZ par défaut, pas de mode pages en V1 ; contrat vérifié par S5 ; purge optionnelle | Tranché (A2, B6, B16), spike S5 |
| Q16 | OIDC | M5 ; forward-auth à secret en V1 | Tranché (A15) |
| Q17 | Exception AGPL | Modèle FSF « controlled interface » ; **provisoire dès le premier commit public**, DCO obligatoire ; relecture avant 1.0 et M6 | Tranché sur le principe (D2) ; relecture ouverte, 1.0 / M6 |
| Q18 | Design | Une direction livrée en 1.0 (A) ; quatre thèmes ensuite (Q74) | Tranché (porteur 2, Q2) |
| Q19 | Series | Entité optionnelle au-dessus de Work | Tranché (A6) |
| Q20 | Compat `api` | N-1 via shim ; 12 mois à valider | Tranché sur le principe (A9), durée M6 |
| Q21 | Sync Kobo | Laissée à Kavita ; émulation M6 seulement si Kavita ne la livre pas ; proxy opt-in par appareil | Tranché (B18) |
| Q22 | Rôle du lecteur | Lecteur pour le contenu non exporté + extraits | Ouvert, M5 |
| Q23 | Étagère | Par Édition | Tranché (A31) |
| Q24 | OPDS propre | Masqué par défaut | Ouvert, M5 |
| Q25 | Clé maîtresse | Générer ; `secrets export` en M1a au format age (scrypt), identique à la sauvegarde M1b ; export forcé dans l'assistant (M3) | Ouvert, M1a |
| Q26 | Comptes Kavita | Un jeton par utilisateur (M4) ; compte Kavita dédié par enfant dès M1b ; correspondance déclarative en M3 | Ouvert, M4 (jetons) ; C10 tranché |
| Q27 | Juriste | En deux temps (E11) : **Q27a** (adaptateurs, posture publique, notification, DCO et exception) avant le premier commit public ; **Q27b** (bibliothèque du foyer, domaine public par pays, fichiers tatoués, CRA, CGU) avant la 1.0 publique | Q27a levée par décision du porteur (2026-11-04), risque accepté ; Q27b ouvert, fin M3 |
| Q28 | Second registre | Codeberg (registre + miroir), Docker Hub en option | Ouvert, M1b |
| Q29 | Emplacement de `/store` | À côté de `/library`, facultatif tant qu'aucune copie conservée | Ouvert, M0 |
| Q30 | Mode passage | Par défaut pour les services connectés ; purge désactivée par défaut ; « Mettre à l'abri » avant purge | Tranché (B6, C6) |
| Q31 | Gallica, Bibebook, ELG | Relire les conditions avant tout plugin ; consigner dans le manifeste | Ouvert, M4 |
| Q32 | Pays de l'instance | Déduit de la locale, confirmé dans l'assistant | Ouvert, M2 |
| Q33 | Romans en cours | `closed_only` par défaut (C20) ; taille de tome par défaut à fixer | Tranché (C20) ; taille ouverte, M2 |
| Q34 | Budget de temps | Hypothèse basse de 6 h par semaine dans `ROADMAP.md`, révisée à la revue de M1a | Ouvert, M0 |
| Q35 | Structure et CRA | Aucune structure avant 1.0 ; revue CRA et avis Q27 (f) avant toute structure | Ouvert, avant toute structure |
| Q36 | Boîte de M1a | 200 h (seuil 300 h), intouchables et coupables écrits ; revue à la 4e semaine | Ouvert, M0 |
| Q37 | Porte P2 / M3-lite | ≥ 2 demandes par mois et préférence pour l'UI ; M3-lite garde le contrôle parental | Ouvert, avant M3 |
| Q38 | Suwayomi `ui_login` | Refus en M1a, compte de service et JWT en M1b | Tranché (E12) ; stable à surveiller |
| Q39 | Fenêtre de maintenance | 4 h (03 h–07 h), ordonnée, interruptible ; fixe en M1a | Tranché (E9) |
| Q40 | Identifiant dans les fichiers | Aucun : manifeste, hash et chemin (ADR-0040) ; lignée dans le miroir de provenance (Q49) | Tranché (passe 6), confirmé (H1) |
| Q41 | `Vol.X` dans le nom | Tome dans ComicInfo seulement pour une série en cours ; chemin figé | Tranché (F7) ; golden M1a |
| Q42 | Boîte de M1a (passe 6) | 200 h conservées, ajouts de schéma absorbés, aucun nouveau coupable | Ouvert, M0 |
| Q43 | Chinois, `all`/`multi` | Écriture obligatoire ; `language_unknown` sinon | Tranché sur le principe (F10) ; alias M1a |
| Q44 | Boîte de M1a (passe 7) | 200 h conservées, 7 ajouts de schéma absorbés, EF-INT-026 intouchable | Ouvert, M0 |
| Q45 | Contenu de `/store` | Toujours l'original ; extraction vérifiée à défaut de source | Tranché (G1) |
| Q46 | Roman web traduit par des équipes | Discriminant selon `translation_nature` : communautaire → Équipe | Tranché (G6) |
| Q47 | Positions hors ligne | `client_ts` borné, historique, conflit proposé | Tranché sur le principe (G10) ; seuil M5 |
| Q48 | Kavita et Komga sur le même arbre | Séparer arbre et indexeurs ; refus des recouvrements d'ici là | Ouvert : ADR avant M4 |
| Q49 | Provenance des fichiers exportés | Miroir par racine physique, rien dans les fichiers (ADR-0046) | Tranché (H1) |
| Q50 | Nommage d'un segment de numérotation | Dossier ou track, tranché par golden `plugin/parse` ; `numbering_conflict` d'ici là | Ouvert : M1a |
| Q51 | Boîte de M1a (passe 8) | 200 h conservées, 2 exigences et volets de schéma absorbés, aucun nouveau coupable | Ouvert, M0 |
| Q52 | Opérations fichier des destinations | Plan sérialisable `list[FsOp]` exécuté par l'hôte | Tranché (H3) |
| Q53 | Contenu exporté non fiable | Dérivé canonique neutralisé à l'ingestion pour les sources `untrusted` (ADR-0047) | Tranché (J1) |
| Q54 | Effacement immédiat | `purge --now`, pierres tombales en base et hors base rejouées après restauration (ADR-0048) ; portées et journal chiffré (Q60, Q64) ; actions réservées à une session fraîche ou à la CLI (Q65) | Tranché (J2, K1, K2, L1) ; levée ouverte, M3 |
| Q55 | Faille de contrôle d'accès Kavita | Catégorie `access_control`, minimum 0.9.1 pour les étagères famille ; passe 10 : toutes les étagères de l'instance en pause dès qu'un compte restreint existe | Tranché (J3), re-tranché (K4) |
| Q56 | Boîte de M1a (passe 9) | 200 h conservées, 2 exigences et 9 volets absorbés, aucun nouveau coupable | Ouvert, M0 |
| Q57 | SVG ou HTML dans un CBZ | Refus en M1a, rastérisation des SVG en M2 | Tranché (J1) |
| Q58 | Boîte de M1a (passe 10) | 200 h conservées, 5 exigences et 3 volets absorbés, aucun nouveau coupable | Ouvert, M0 |
| Q59 | Identité d'un chapitre Suwayomi | `sourceId` + URL du chapitre ; entier local en cache ; réinstallation simulée au kit | Tranché (K3) |
| Q60 | Pierre tombale : portée et journal | Portées `variant`/`place`/`series_binding`/`work` ; journal signé remplacé en passe 11 par des valeurs brutes chiffrées, ligne indéchiffrable inerte (Q64) | Tranché (K1, K2), re-tranché (L1) |
| Q61 | Sessions et ré-authentification | 30 jours glissants par défaut ; admin 30 min / 12 h dans le profil durci ; `reauth-required` à 900 s sur les actions irréversibles, les secrets et le contrôle parental quand un profil restreint existe | Tranché (K5), re-tranché par le porteur (P2, P3, Q72) |
| Q62 | Original `untrusted` en mode passage | Gardé dans `/store` ; sans `/store`, correction depuis le dérivé | Tranché (K9) |
| Q63 | Seul admin indisponible | Kit de secours imprimable sans aucune clé, qui référence l'export chiffré du trousseau (passes 11 et 12), second admin facultatif, aucun code web | Tranché (K7), M3 |
| Q64 | Pierres tombales : confidentialité et migrations | Valeurs brutes chiffrées (AEAD), re-normalisées sous chaque version citée ; risque accepté : trousseau perdu ⇒ journal inerte | Tranché (L1) |
| Q65 | Clés d'API et actions irréversibles | Axe crédential : actions réservées à une session fraîche ou à la CLI locale ; clés `admin` à expiration obligatoire | Tranché (L2) |
| Q66 | Âge dans les EPUB et échelle | Aucun âge écrit à l'export ; entier minimal + drapeau X, palier 12 | Tranché (L5) |
| Q67 | Boîte de M1a (passe 11) | 200 h conservées, 2 exigences, 1 avancée et des volets absorbés, aucun nouveau coupable | Ouvert, M0 |
| Q68 | Version minimale de Kavita | Minimum unique 0.9.1 ; 404 sur une route de la liste ⇒ `degraded_feature` (`target.route_missing`, passe 13) ; routes testées contre l'`openapi.json` de chaque version, nightly {minimum, épinglée, dernière} | Tranché (M9) |
| Q69 | Ligne au `kid` inconnu au rejeu | `kid` cité jamais retiré, rotation sans réécriture ; ligne au `kid` inconnu bloquante jusqu'au réimport ou à la dérogation « trousseau perdu » | Tranché (M1) |
| Q70 | Ligne au `kid` inconnu : sauvegarde ancienne ou ligne forgée | Registre des `kid` émis (base et sentinelle) : `kid` enregistré ⇒ blocage, `kid` absent du registre ⇒ ligne forgée inerte | Tranché (N1) |
| Q71 | Transport `push` des destinations | `ApiCall.body` (JSON ou référence de contenu) soumis aux règles de `FsOp.write` ; `push` réservé jusqu'à M4 ; Calibre par `/cdb/*` ; Audiobookshelf jamais par l'upload | Tranché (N6), avant M4 |
| Q72 | Sécurité proportionnée | Protections invisibles toujours actives ; profil durci facultatif ; sessions 30 j ; ré-authentification 900 s sur actions irréversibles, secrets, contrôle parental si profil restreint ; clés à expiration facultative ; forward-auth désactivé par défaut | Tranché (porteur 1, P1–P6) |
| Q73 | Passkeys et bibliothèque d'auth | Passkeys WebAuthn en M1b (mot de passe en repli) ; briques Python, better-auth écartée | Tranché (porteur 1, P7, P8) |
| Q74 | Thèmes du front | Socle unique, quatre thèmes (jetons + variantes fermées), A « Bibliothèque » par défaut, A en M3, B/C/D en M4 | Tranché (porteur 2, Q2) |
| Q75 | Suivi automatique | Suivi par Édition, liaisons classées, repli notifié, trous, cadence adaptative, filtres ; schéma M1a, M2, UI M3 | Tranché (porteur 2, Q3) |

## Arbitrage du porteur (2026-11-04)

Le porteur délègue les questions ouvertes : **la recommandation inscrite pour chaque Q est retenue** (Q1 à Q71), y compris la boîte M1a de 200 h. Q27a (relecture juridique avant le premier commit public) a été levée par décision du porteur le 2026-11-04 : publication directe, risque accepté et consigné. Le choix de la direction de front reste à faire par le porteur. **Mise à jour (arbitrage porteur 2)** : direction tranchée : A « Bibliothèque » par défaut, quatre thèmes au choix (Q74) ; Python confirmé ; suivi automatique complété (Q75).
