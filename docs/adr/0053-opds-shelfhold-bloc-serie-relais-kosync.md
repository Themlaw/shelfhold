---
status: proposée
date: 2026-11-15
decision-makers: mainteneur
---
# ADR-0053 — OPDS Shelfhold avec bloc série, et relais kosync vers Kavita

## Contexte et problème
Le porteur lit sur KOReader. Par l'OPDS de Kavita, un manga ou un webtoon rangé chapitre par chapitre ne se télécharge qu'un chapitre à la fois : pour 200 chapitres, 200 téléchargements. Il veut récupérer une série **d'un seul bloc**, avec une progression qui reste synchronisée avec Kavita (O4, EF-EXP-026).

Le plan de M4 y répond par le lot L10 (EF-EXP-019) : Shelfhold **assemble** les chapitres en un seul CBZ **dans la bibliothèque Kavita**, et les anciennes entrées passent en corbeille. Le porteur juge cette voie risquée : elle modifie ce que Kavita a scanné, fait disparaître la granularité par chapitre côté Kavita (la progression d'un chapitre devient la page d'un gros fichier, par projection), et son défaut d'exécution touche l'intégrité des fichiers des membres.

La spec prévoit déjà ce qu'il faut pour une autre voie, mais en M5 : OPDS sortant (EF-INT-007), kosync Shelfhold (EF-LEC-009), écran « Connecter un appareil » (EF-UX-004), jetons d'appareil (EF-USR-022, ADR-0049 ; colonnes posées en M3, jetons émis en M5), hash KOReader et `document_alias` (ENF-QUAL-005, L13 déjà fusionné). Le préalable de la synchronisation avec Kavita (L12, L14a, L14b) est prévu en M4. Cet ADR avance une **tranche** de M5 pour obtenir le bloc sans toucher à Kavita.

## Facteurs de décision
- Ne rien écrire dans la bibliothèque Kavita pour obtenir un bloc : le bloc est un artefact de lecture, pas un fichier de la collection.
- Un fichier de bloc **déterministe** : à contenu égal, mêmes octets, donc même empreinte KOReader.
- Contrôle parental sur toutes les surfaces (EF-USR-006, ENF-SEC-010) : OPDS et kosync en font partie.
- Pas de nouveau mot de passe faible (EF-LEC-009, passe 11 : jamais le mot de passe du compte).
- Réutiliser L12, L13, L14a, L14b au lieu d'ajouter un second chemin d'écriture vers Kavita.
- Boîte de temps de M4 (point de contrôle à 353 h, PLAN-M4 §7).

## Ce que disent les sources (vérifié)
Lecture des sources le 2026-11-15 : KOReader `master` 457a968 (`plugins/kosync.koplugin/`), Kavita `develop` e844b64.

**KOReader, client kosync** (`KOSyncClient.lua`, `main.lua`, `api.json`)
- En-têtes : `accept: application/vnd.koreader.v1+json`, `x-auth-user`, `x-auth-key` (`KOSyncClient.lua` l. 30-35). Le client envoie `md5(mot de passe)` comme clé (`main.lua` l. 568, 606) : la clé est un équivalent de mot de passe rejouable, pas un secret dérivé.
- Routes (`api.json`) : `POST /users/create` (201 ou 402), `GET /users/auth` (200 ou 401), `PUT /syncs/progress` (200, 202 ou 401), `GET /syncs/progress/:document` (200 ou 401). Le corps du `PUT` porte `document`, `progress`, `percentage`, `device`, `device_id` et `metadata` en option. **Il n'envoie pas d'horodatage** : l'horloge est celle du serveur.
- `progress` est `tostring(progress)` (`KOSyncClient.lua` l. 130). Pour un document **paginé** (CBZ, PDF), `getLastProgress()` renvoie le numéro de page courant, **à partir de 1** (`readerpaging.lua` : `getTopPage()`) ; pour un document **refluable** (EPUB), c'est un xpointer (`main.lua` l. 650-663). À la lecture d'une position distante, un document paginé fait `GotoPage(tonumber(progress))`, un document refluable `GotoXPointer(progress)` (`main.lua` l. 705-712).
- `document` est `partial_md5_checksum` du fichier, ou, si l'utilisateur a choisi la méthode « nom de fichier », `md5(nom du fichier)` (`main.lua` l. 666-681).
- À la lecture, si le serveur ne renvoie pas de `percentage`, KOReader affiche « aucune progression » (l. 851) ; si `percentage` ou `progress` sont égaux aux siens, il ne fait rien (l. 877) ; sinon il compare `timestamp` du serveur à son dernier tour de page (l. 897-903).

**Kavita, côté serveur** (`KoreaderController.cs`, `KoreaderService.cs`, `KoreaderHelper.cs`, `AuthKeyAuthenticationHandler.cs`)
- **Kavita ignore `x-auth-user` et `x-auth-key`.** La clé d'API de l'utilisateur est un segment de l'URL : `GET {apiKey}/users/auth`, `PUT {apiKey}/syncs/progress`, `GET {apiKey}/syncs/progress/{ebookHash}` (`KoreaderController.cs` l. 22-58) ; l'extraction de la clé accepte la requête, l'en-tête ou la valeur de route `apiKey` (`AuthKeyAuthenticationHandler.cs` l. 113-131).
- Hash inconnu : `400` avec un message (l. 38-39 du service, l. 82 du contrôleur), pas `404` ni corps vide.
- Lecture : pour un non-EPUB, `progress = PageNum + 1` (« KOReader est indexé à partir de 1 », `KoreaderService.cs` l. 94-95) ; pour un EPUB, un xpointer fabriqué par `GetKoreaderPosition` (`KoreaderHelper.cs` l. 166). `percentage = PageNum / file.Pages`, `timestamp = LastModifiedUtc` de Kavita.
- Écriture : `UpdateProgressDto` (`KoreaderHelper.cs` l. 85-116) : un nombre seul, c'est « une archive ou un PDF » et `PageNum = n - 1` ; un `#_doc_fragment_N` ou un chemin `/body/DocFragment[N]…` donne la page (fragment) et, si possible, un `BookScrollId`. `percentage`, `device` et `device_id` sont ignorés (« uniquement pour maintenir l'interface », `KoreaderBookDto.cs`).
- Kavita retrouve le fichier par **son propre** hash (`GetByKoreaderHash`), calculé au scan. Variante de L13 : hexadécimal en majuscules et 11 échantillons, donc identique à KOReader jusqu'à 1 Gio (`docs/decisions/M4.md`, L13).

Conséquence : le format de `progress` pour un document non refluable est bien « numéro de page, à partir de 1 », des deux côtés ; l'authentification, elle, n'est **pas** la même (en-têtes pour KOReader, segment d'URL pour Kavita). Shelfhold parle l'en-tête à KOReader et la clé Kavita du membre (L12) à Kavita.

## Options considérées
### A. Assemblage dans Kavita (L10 seul)
- Avantages : un seul serveur ; le bloc est lu par Kavita comme par KOReader (OPDS et kosync de Kavita) ; rien de nouveau à exposer.
- Inconvénients : modifie la bibliothèque (remplacement de N entrées par une), irréversible dans les faits au-delà de la corbeille ; la granularité par chapitre côté Kavita (marqueurs lu, « suivant ») est perdue ; chaque nouveau chapitre impose un réassemblage et un rescan ; le risque d'intégrité se paie à chaque série suivie.

### B. OPDS Shelfhold avec bloc virtuel, relais kosync (cette ADR)
- Avantages : Kavita n'est pas modifié ; la granularité par chapitre reste intacte et reste la source de vérité de la progression ; un bloc se refabrique sans effet sur la collection ; la tranche réutilise des lots déjà prévus.
- Inconvénients : Shelfhold sert un service de plus à des appareils (surface d'attaque, charge) ; une traduction de position à écrire et à tester ; une tranche de M5 avancée en M4.

### C. Les deux
- Avantages : couvre aussi le besoin « un tome » dans la bibliothèque Kavita (EF-EXP-019 tel que rédigé).
- Inconvénients : double travail et double risque ; L10 reste le lot le plus risqué du plan et n'est plus nécessaire au besoin exprimé.

## Décision
**Option B. L10 n'est plus nécessaire au besoin du porteur ; il passe en option (voir « Ce que devient L10 »).**

### 1. Flux OPDS Shelfhold (OPDS 1.2, Atom)
- Racine, étagères, séries, chapitres, tous filtrés par le profil du jeton (voir « Contrôle parental »).
- Chaque série porte une entrée **« Série complète »** (un acquisition link vers le bloc) et des **tranches** en option (plage de chapitres, « à partir du premier non lu »). Une tranche est un bloc comme un autre, avec ses propres membres.
- Le bloc est un **CBZ virtuel** : aucune copie complète sur disque. Les octets sont produits à la lecture à partir des fichiers **déjà exportés** (les Variantes courantes des Unités), jamais depuis un contenu non exporté.

### 2. Fichier déterministe
Un zip **stocké** (méthode 0), sans descripteur de données, dont tous les champs variables sont fixés :
- ordre des entrées : ordre de lecture des Unités, puis ordre des pages de chaque Unité ; noms ASCII `{rang de l'Unité sur 5 chiffres}/{page sur 5 chiffres}.{extension}` ;
- date et heure DOS fixées au 1980-01-01 00:00:00 ; attributs, version et indicateurs fixés ; aucun commentaire ; Zip64 seulement si la taille ou le nombre d'entrées l'impose, de façon déterministe ;
- taille, CRC-32 et tailles non compressées des entrées sont connus **avant** de produire un octet : ils viennent du répertoire central de chaque CBZ source. La taille totale, donc `Content-Length`, est exacte sans fabriquer le fichier.

**Piège de l'empreinte partielle, à traiter dès la conception.** L'empreinte KOReader ne lit que 12 échantillons de 1 Kio, aux offsets 0, 1 Kio, 4 Kio … 256 Mio, 1 Gio, et s'arrête au premier échantillon vide (`docs/decisions/M4.md`, L13). Deux versions d'un bloc dont un chapitre a été **ajouté à la fin** ont les mêmes octets à chaque offset échantillonné, tant que les deux fichiers atteignent les mêmes offsets : **même empreinte pour deux contenus différents**. Un `document_alias` par version n'y suffirait pas, puisque la clé serait la même. Parade : l'entrée n° 1 du zip porte, dans le **champ « extra » de son en-tête local** (identifiant privé, ignoré des lecteurs conformes), l'empreinte complète de la version (SHA-256 de la liste ordonnée `(Unité, content_hash)` et des paramètres de rendu). Cet en-tête est dans le premier Kio : l'échantillon 0 change à chaque version. Le contenu reste une fonction pure de la liste des membres. À vérifier au lot : ouverture dans KOReader et Kavita avec ce champ, et test `test_bundle_versions_always_have_distinct_partial_md5` (ajout en fin, retrait au milieu, remplacement d'un chapitre au-delà de 256 Mio).

### 3. Authentification : un jeton d'appareil, pas un mot de passe
- Le jeton est celui d'EF-USR-022 / ADR-0049 : émis **pour un profil lecteur** (`subject_user_id`), par un admin, en action à fraîcheur d'authentification. Avancé en M4 (migration seulement si les colonnes de M3 ne suffisent pas).
- Il est **généré**, jamais choisi : un identifiant public court (`x-auth-user` ; nom d'utilisateur HTTP Basic) et un secret de **128 bits** (affiché une seule fois, groupes de 4 caractères de l'alphabet base32 sans ambiguïté). Aucun mot de passe de compte n'est accepté sur ces routes (EF-LEC-009, passe 11).
- **KOReader pour kosync** : `x-auth-key` vaut `md5(secret)`. Cette empreinte est un équivalent de mot de passe rejouable, mais avec 128 bits aléatoires elle ne se devine pas hors ligne. Le serveur ne stocke que `HMAC-SHA-256(clé serveur, md5(secret))`, comparé en temps constant ; l'équivalent MD5 du mot de passe principal n'existe nulle part.
- **KOReader pour l'OPDS** : HTTP Basic, avec le même identifiant et le même secret (champs de l'écran de catalogue de KOReader). Un seul jeton pour les deux usages, une saisie dans deux écrans.
- **TLS obligatoire** : Basic et `x-auth-key` ne s'envoient pas en clair. Le HTTP simple n'est accepté que par un réglage explicite de l'admin, avec un avertissement sur l'écran « Connecter un appareil ».
- **Portée fermée** : le jeton ouvre le flux OPDS, les routes kosync et les téléchargements de blocs. Toute autre route (API, SSE, vignettes de l'UI) le refuse (table de vérité d'ENF-SEC-010 : colonne « jeton d'appareil »). Aucune action réservée (EF-USR-024).
- `users/create` répond 402 (`api.json` l'attend) ; `users/auth` répond 200 ou 401, jamais de détail sur la cause.
- Le choix « sans mot de passe à saisir » de Kavita (clé dans l'URL) est écarté : l'URL finit dans les journaux de proxy et dans l'historique de KOReader. Il reste une option si la saisie du secret sur liseuse s'avère trop pénible (point à trancher).

### 4. Relais kosync
Shelfhold sert `users/create`, `users/auth`, `syncs/progress` et `syncs/progress/:document`. Il est la **source de vérité locale** : une position reçue est écrite tout de suite dans `reading_position` (ancre sur le fichier lu, EF-LEC-014), puis transmise à Kavita par le lot de synchronisation, avec le jeton Kavita **du propriétaire du jeton d'appareil** (L12).

**Résolution du `document`.** Dans l'ordre :
1. `document_alias` (fichier unique exporté) → `export_item` (ENF-QUAL-005 ; 409 si le même hash existe dans deux supports) ;
2. empreinte d'un bloc (`bundle_version`, ci-dessous).
Un hash inconnu **et** un hash visible seulement à un autre profil répondent de la même façon (aucun oracle d'existence). Réponse à une lecture sans position : `200` et `{}` (KOReader dit alors « aucune progression », `main.lua` l. 851). Kavita, lui, répond `400` : on ne le copie pas.

**Bloc : modèle.** `document_alias` ne vise que des `export_item` (EF-LEC-014 ; un alias ne vise jamais un autre support). Un bloc n'étant pas un `export_item`, on ajoute des tables **sœurs** plutôt que d'élargir la clé de l'alias :
- `bundle_version(id, edition_id, visible_set_digest, version_digest, koreader_partial_md5, page_count, size, created_at)` ;
- `bundle_member(bundle_version_id, ordinal, export_item_id, content_hash, first_page, page_count, page_map_state)`.
Une ligne par chapitre, pas par page. Les lignes d'une ancienne version **restent** (quelques centaines d'octets) : elles servent à traduire une position prise sur l'ancien fichier, même quand ses octets ne peuvent plus être servis. L'amendement d'EF-LEC-014 est limité à cette phrase : « kosync résout un `export_item` ou une `bundle_version` ».

**Sens KOReader → Kavita (bloc).** La page N (à partir de 1) est traduite en `(chapitre, page dans le chapitre)` par `bundle_member`. La position est stockée sur l'`export_item` du chapitre (ancre réelle, `content_hash` du membre), jamais sur le bloc : la vérité reste exprimée en fichiers que Kavita connaît. Les chapitres antérieurs à celui-ci sont marqués lus (même règle que la projection de L14a), les suivants ne bougent pas. Puis le job de L14b pousse vers Kavita (LWW sur l'horloge du serveur, jamais en arrière).

**Sens Kavita → bloc.** Pour la version que KOReader interroge : on prend la position la plus avancée parmi les chapitres du bloc (règle de L14a), `page = first_page(chapitre) + page_dans_chapitre`, `percentage = page / page_count`, `timestamp` = l'horloge du serveur qui a reçu la position la plus récente. Le chapitre non lu en premier donne la page `first_page`.

**Chaque version garde sa correspondance.** Une position prise sur la version V1 se traduit par V1 (`bundle_member` de V1) en positions de fichiers ; elle se relit sur V2 par V2. Si le chapitre a été remplacé entre-temps (nouveau `content_hash`), I30 s'applique : position transposée, marquée `approximate`, journalisée. Les bornes `start_locator`/`end_locator` d'`unit_coverage` (L14a) servent de repli quand `page_map_state` d'un membre est `unverified`.

**Fichier unique existant dans Kavita (chapitre seul).** Le relais transmet la page telle quelle (traduction identique : un seul membre). Le hash côté Kavita est `document_alias.kavita_md5` (L13).

### 5. Latence et conflits
- `PUT` : écriture locale, `200` immédiat, jamais bloqué par Kavita. Poussée **débouncée à 60 s** par `(profil, chapitre)` (D11 de PLAN-M4), mutualisée avec L14b.
- Kavita en panne : le job se met en pause (L14b), aucune erreur côté KOReader.
- `GET` : on sert la position locale fusionnée. Si le dernier tirage de ce membre date de plus de 60 s, on tente un **tirage borné (1,5 s)** de Kavita ; au-delà, on répond avec le local et on tire en arrière-plan. Sans ce tirage, une lecture faite dans Kover resterait invisible jusqu'à 15 min.
- **Conflit : dernière écriture sur l'horloge du serveur**, comme L14b : une position reçue de KOReader porte l'heure de Shelfhold (le client n'envoie pas d'horodatage, voir plus haut), une position tirée de Kavita porte celle de Kavita ; une dérive de plus de 5 min est signalée dans Santé (D11). `reading_progress` retient la plus avancée par Unité (L14a), mais **la position renvoyée à KOReader est la dernière**, pas la plus avancée : relire un chapitre précédent doit se retrouver à la prochaine ouverture. Conséquence assumée : revenir en arrière dans KOReader ne recule pas Kavita (L14b).

## Cas limites
- **Chapitre retiré ou remplacé dans un bloc déjà téléchargé.** L'appareil garde l'ancien fichier ; sa version reste résoluble (lignes conservées). Un chapitre *retiré* : une position dessus n'a plus de cible, elle est journalisée `orphan`, non poussée, et les chapitres lus avant restent lus. Un chapitre *remplacé* : voir I30 ci-dessus. L'URL du bloc est **versionnée** (`…/bundle/{version_digest}.cbz`) : si des membres ont disparu, `410 Gone`, et le flux propose la version courante ; `If-Range` avec un ancien `ETag` renvoie la version courante entière.
- **Préférence d'équipe.** Le bloc suit les Variantes exportées, donc l'équipe préférée courante. Un changement d'équipe (L35) change la liste `(Unité, content_hash)` : nouvelle version, ancienne conservée pour les traductions, positions `approximate` pour les Unités concernées. Un bloc par Édition ; l'Édition préférée est celle du flux par défaut.
- **Bloc de 2 Gio.** Diffusion en flux par morceaux de 64 Kio à partir des fichiers sources, descripteurs ouverts pour la durée du flux (un remplacement atomique par `rename` laisse l'ancien inode lisible). `Range` à plage unique (`206`), `ETag` = `version_digest`, `Accept-Ranges: bytes`. Un saut dans une entrée déflatée coûte une décompression de **cette image** seulement. Disque : **zéro** octet supplémentaire par défaut (le bloc est virtuel, l'empreinte se calcule par lecture des 12 échantillons sur le même lecteur positionnable que L13). Cache de repli sur disque (LRU, plafond réglable, désactivé par défaut) seulement si la mesure montre un goulot de CPU. Charge : au plus 2 flux de bloc simultanés pour l'instance, 1 par jeton, sinon `503` avec `Retry-After`. Plafond d'un bloc : **2 Gio**, sous la limite FAT32 de 4 Gio des liseuses et sans Zip64 ; au-delà, découpe automatique en « Série complète (1/N) », chaque partie ayant sa propre version (point à trancher). Je ne prétends pas que KOReader reprend un téléchargement interrompu par `Range` : non vérifié, et sans effet sur la conception.
- **Webtoon sans pagination stable.** La page est l'index de l'image dans le fichier de l'Unité, dans l'ordre que Kavita utilise pour numéroter ses pages ; tant que le fichier ne change pas, la pagination est stable. Si la source redécoupe un chapitre, son `content_hash` change : nouveau membre, I30. Si le nombre de pages d'un membre n'est pas établi de la même façon des deux côtés (`page_map_state = unverified`), la traduction **ne descend pas** à la page : elle se limite au chapitre (lu ou en cours, sans page). Aucune page inventée n'est poussée à Kavita.
- **EPUB de roman (`progress` = xpointer).** **Traduction hors périmètre**, passage transparent **traité** pour les fichiers qui existent aussi dans Kavita : le `progress` reçu est stocké tel quel comme `locator` opaque (aucun xpointer n'est analysé par Shelfhold) et transmis à la route kosync de Kavita avec la clé Kavita du membre ; Kavita le convertit (`KoreaderHelper.UpdateProgressDto`) ; en sens inverse, la chaîne de `GetKoreaderPosition` est renvoyée telle quelle. Aucun bloc d'EPUB : on ne fusionne pas des romans. Ce passage ajoute les routes kosync de Kavita à la liste fermée du jeton Kavita d'un membre (L12, D8) : lot séparé, facultatif.
- **Méthode « nom de fichier » de KOReader** (`main.lua` l. 666-681) : non prise en charge (le nom d'un bloc est dérivé de la version, l'utilisateur peut le renommer). L'écran « Connecter un appareil » demande la méthode binaire.

## Contrôle parental
- Le jeton appartient à un profil ; le flux, les blocs et kosync appliquent **la portée de ce profil** (EF-USR-006) : un enfant ne voit rien au-dessus de son plafond, un non classé est invisible, et `shelf.allowed_profiles` s'applique si D-P3 est tranchée.
- **Composition du bloc par profil.** Les membres sont les Unités visibles pour le profil. `visible_set_digest` entre dans `version_digest` : deux profils qui voient le même ensemble partagent la version, les autres non. Le premier Kio, `ComicInfo.xml` s'il y en a un, et les noms d'entrée **ne mentionnent aucune Unité masquée**.
- Les compteurs du flux (`count`, nombre de chapitres, taille) ne portent que sur le visible. Une série entièrement masquée n'a ni entrée ni « Série complète ».
- Tout est ajouté à `test_unrated_invisible_to_child_on_all_surfaces` et au méta-test des surfaces dans la PR qui crée la route (PLAN-M4 §2).

## Sécurité
- **SSRF : aucune surface nouvelle.** KOReader n'envoie aucune URL à suivre. Le relais ne parle qu'à la destination Kavita déclarée, par le Fetcher et sa garde SSRF ; `document` n'accepte que l'hexadécimal de 32 caractères ; `metadata` est ignoré, jamais relayé ni journalisé.
- **Fuite de titres par le flux.** Tout le flux est derrière le jeton ; aucune page publique. Les titres n'apparaissent pas dans les URL, ni dans les journaux (identifiant du jeton tronqué, jamais le secret ni `x-auth-key`). `Content-Disposition` et `Cache-Control: private`, `Vary: Authorization`. Vignettes : autorisation **avant** l'`ETag` (ENF-SEC-010).
- **Jeton volé.** Le jeton ouvre au plus ce que **son** profil lit et permet d'écrire **sa** progression : aucune action réservée, aucune écriture de bibliothèque. Révocation immédiate (UI, CLI), `last_used_at` affiché, secret jamais relisible. Le secret ne vaut que pour le profil désigné ; un jeton d'enfant ne devient jamais plus qu'un jeton d'enfant.
- **Limitation de débit.** Échecs d'authentification : 5 par minute par couple (adresse, identifiant), recul exponentiel, réponse **identique** pour identifiant inconnu et clé fausse. Jetons valides : plafonds par jeton (flux et kosync) et sémaphores pour les blocs (voir plus haut). Un `Range` ne relance aucune fabrication.
- **Déni de service par fabrication.** Aucune fabrication complète : un bloc se lit en flux ; la charge est bornée par les sémaphores et par la taille maximale.
- **Journaux** : jamais le secret, `x-auth-key`, le corps du `PUT` ni la clé Kavita d'un membre (L12).

## Ce que devient L10
- **EF-EXP-019 est une exigence de M4** dans 01 ; L10 ne peut donc pas disparaître sans amendement de la spec, que seul le porteur décide.
- **Recommandation** : L10 passe en **coupable de premier rang**, avant L22, L37 et L32 (D-P2), avec EF-EXP-019 redatée « Plus tard » (`[[deferred]]` écrit, accord écrit du porteur, comme D9) tant que personne ne demande l'assemblage **dans** la bibliothèque. Les lots de la tranche (ci-dessous) couvrent le besoin « un seul bloc » sans lui. Si le porteur veut garder l'option C, L10 reste tel que prévu (🔒, migration 0069) ; aucun lot ci-dessous n'en dépend.
- **Effet sur les autres lots** : L16 liste L10 en dépendance pour l'ordre de `planner.py`/`pipeline.py` (point chaud), pas pour un contrat ; retirer L10 allège le chemin critique d'un lot et libère 0069. EF-BIB-044 (« assemblage par tome », L06a/L10/L15) perd son seul cas d'assemblage et garde les autres.

## Lots proposés
Numérotation à la suite de PLAN-M4 (L00 à L45), à inscrire dans `~/shelfhold-spec/PLAN-M4.md` après accord du porteur ; je n'ai pas modifié ce fichier.

| Lot | Contenu | Périmètre | Dép. | Migration | Marque |
|---|---|---|---|---|---|
| **L46** | Jetons d'appareil (EF-USR-022, tranche) | émission par un admin pour un profil, secret généré affiché une fois, `HMAC` stocké, authentification `x-auth-user/x-auth-key` et Basic, limitation de débit, révocation, journaux expurgés ; colonne « jeton d'appareil » de la table de vérité | L02 | 0076, seulement si les colonnes de M3 ne suffisent pas | 🔒 (secrets, contrôle parental) |
| **L47** | Routes kosync locales | 4 routes, résolution `document_alias`, écriture `reading_position`, lecture de la position fusionnée, 409 sur hash ambigu, `{}` sans position, sans relais | L13, L14a, L46 | — | 🔒 (données des membres) |
| **L48** | Flux OPDS 1.2 filtré par profil | navigation étagères, séries, chapitres, téléchargement d'un chapitre ; zéro fuite pour l'enfant | L46 | — | 🔒 (contrôle parental) |
| **L49** | Bloc virtuel déterministe | zip stocké, extra d'identité, `bundle_version`, `bundle_member`, calcul de l'empreinte par lecteur positionnable, `Range`, `410` versionné, sémaphores, tranches | L13, L48 | 0077 | 🔒 (déterminisme, intégrité) |
| **L50** | Relais vers Kavita | traduction page ⇄ chapitre/page, projection des chapitres antérieurs, tirage borné à la lecture, poussée débouncée, `orphan`, I30 | L12, L14a, L14b, L47, L49 | — | 🔒 (données des membres, secrets) |
| **L51** | Passage d'EPUB et écran « Connecter un appareil » | `locator` opaque, routes kosync de Kavita ajoutées à la liste fermée de L12, écran d'EF-UX-004 (méthode binaire, TLS) | L12, L46, L50 | — | 💪, facultatif (EPUB) |

- **Estimation** : environ 33 h au total (L46 ≈ 5, L47 ≈ 5, L48 ≈ 5, L49 ≈ 8, L50 ≈ 7, L51 ≈ 3). L10 retiré rend 6 h ; reste ≈ +27 h sur la boîte.
- **Placement** : après L17 (point chaud `reading/` : L14a → L14b → L15 → L17 → L16) et après le relevé de vélocité de V8 ; L46 à L48 sont indépendants de L12 et peuvent partir plus tôt si un créneau le permet.
- **Preuves** : vecteurs de requêtes relevés dans `KOSyncClient.lua` et `api.json` (en-têtes, corps, codes) rejoués en CI ; l'équivalent de D-P5 : une lecture réelle dans KOReader du porteur (une série de 3 chapitres), faute de quoi le volet « appareil réel » passe en `[[partial]]`.
- **Spec à amender si l'ADR est acceptée** : EF-LEC-009, EF-INT-007 et EF-UX-004 (tranche M4 / reste M5), EF-LEC-014 (phrase sur la résolution), ADR-0049 (jetons émis en M4 pour OPDS et kosync).

## Conséquences
- Positives : un seul téléchargement pour une série entière ; Kavita et la collection ne sont pas modifiés ; la progression reste exprimée par fichier et par chapitre, donc juste des deux côtés ; un jeton par appareil, sans mot de passe réutilisé ; L10, le lot le plus risqué, devient inutile à ce besoin.
- Négatives : Shelfhold sert une surface réseau de plus à des appareils (jetons, limitation, charge) ; une traduction de position à tenir exacte ; une tranche de M5 avancée en M4 (≈ +27 h) ; EF-EXP-019 reportée si le porteur suit la recommandation.

## Risque principal
Une traduction fausse qui marque comme lus des chapitres non lus, ou deux versions de bloc qui partagent une empreinte. Mitigations : l'identité de version dans le premier Kio du fichier avec son test dédié, le stockage de la position sur le fichier réel du chapitre, `page_map_state` qui interdit toute page inventée, et la règle « jamais en arrière » de L14b.

## Réversibilité
Haute. Le bloc est virtuel (rien n'est écrit dans la collection), les tables `bundle_*` se recalculent à partir de `export_item` et de `unit_coverage`, et le jeton d'appareil se révoque. Désactiver le flux ne perd aucune position : elles sont ancrées sur les fichiers.

## Contradicteur
Relecture adverse du 2026-11-15, sur les mêmes révisions que l'ADR (KOReader `master` 457a968, koreader-base 823981c qui embarque MuPDF 1.28.6 avec ses correctifs, MuPDF `1.28.6`, Kavita `develop` e844b64) et sur le code de Shelfhold de cette branche. La décision n'est pas modifiée ; chaque point demande une réponse ou un amendement avant acceptation.

**Verdict : option B défendable, ADR non acceptable en l'état.** Un point bloquant (l'empreinte mise en cache par KOReader rend la parade du champ « extra » inopérante dans le cas nominal), six majeurs, sept mineurs.

### Ce qui tient (vérifié)
- **Échantillon 0.** `util.partialMD5` (`frontend/util.lua` l. 1094-1110) fait `seek(lshift(1024, 2*i))` pour i = -1 … 10 ; en LuaJIT le décalage -2 est pris modulo 32 (30), d'où l'offset 0. Les offsets sont donc 0, 1 Kio, 4 Kio … 256 Mio, 1 Gio : douze échantillons, le premier couvre les octets 0 à 1023. L'en-tête local n° 1 (30 octets + nom de 15 octets + extra de 4 + 32 octets) y tient largement. La collision « chapitre ajouté à la fin » décrite par l'ADR est réelle.
- **MuPDF accepte l'extra arbitraire et le Zip64.** `read_zip_entry` (`source/fitz/unzip.c` l. 439-461) lit `extralength` et le saute sans l'interpréter ; le CRC-32 est lu puis ignoré (l. 344, 455) ; le Zip64 (localisateur, fin de répertoire, extra `0x0001`) est pris en charge (l. 285-303, 370). Les noms et l'ordre des pages viennent du répertoire central.
- **Format de `progress`.** `ReaderPaging:getLastProgress()` = `getTopPage()` et `getLastPercent()` = `current_page / number_of_pages` (`readerpaging.lua` l. 175-183) : page à partir de 1, cohérent avec `percentage = page / page_count`.

### Bloquant
**BLQ-1. KOReader ne recalcule jamais l'empreinte d'un fichier déjà ouvert : la version n'est pas lue dans le fichier mais dans le fichier annexe.**
- `ReaderUI` n'appelle `util.partialMD5` que si `partial_md5_checksum` manque dans les réglages du document (`frontend/apps/reader/readerui.lua` l. 497-500) ; kosync envoie cette valeur mémorisée (`plugins/kosync.koplugin/main.lua` l. 674-676, `getFileDigest` = `doc_settings:readSetting("partial_md5_checksum")`). Par défaut (`document_metadata_folder = "doc"`), le fichier annexe est `<chemin sans suffixe>.sdr` (`frontend/docsettings.lua` l. 118-139) : il est lié au **chemin**, pas au contenu.
- L'OPDS de KOReader nomme le fichier d'après l'entrée : `auteur - titre.cbz` (`opdsbrowser.lua` l. 1443-1452), ou, en mode « noms du serveur », d'après `Content-Disposition` (l. 638-671). Télécharger la version V2 de « Série complète » donne **le même chemin** que V1 ; KOReader propose « Écraser » (l. 1481-1511), réécrit le fichier (`io.open(local_path, "w")`, l. 1513-1531) et ne touche pas au `.sdr`.
- Conséquence : à l'ouverture de V2, KOReader envoie l'empreinte de **V1**. Shelfhold traduit les pages de V2 avec la table de V1. Dès qu'un chapitre a été inséré, retiré ou remplacé avant la position courante, il marque lus les mauvais chapitres, chez Kavita, et sans retour (L14b : jamais en arrière). C'est exactement le « risque principal » de l'ADR, et la parade du premier Kio ne le couvre pas : elle ne joue que pour un fichier jamais ouvert à ce chemin.
- Amendement demandé : (a) le **titre de l'entrée OPDS** et le `Content-Disposition` portent un suffixe de version court et stable (par exemple `Titre — Série complète [a3f9c2].cbz`), pour qu'une nouvelle version ait toujours un nouveau chemin ; (b) le serveur, recevant une empreinte de version V pour un `device_id` qui a déjà poussé sur V′ plus récente, ne fait aucune projection « chapitres antérieurs lus » et journalise ; (c) test `test_redownloaded_bundle_under_same_path_never_projects_with_old_map` ; (d) relevé sur appareil : écraser V1 par V2 et vérifier l'empreinte envoyée. Le champ « extra » reste utile (deux fichiers neufs ne partagent jamais d'empreinte) mais n'est pas la parade principale.

### Majeurs
**MAJ-1. La page N du bloc n'est pas la page N de Kavita pour des formats ou des noms que Shelfhold accepte.**
- Formats : Shelfhold accepte en page JPEG, PNG, WebP, GIF, **AVIF et JPEG XL** (`infrastructure/content_trust/cbz_entries.py` l. 3, l. 28 ; import Suwayomi `tree_import.py` l. 85-86). Kavita compte `png|jpeg|jpg|webp|gif|avif` (`Parser.cs` l. 32, `IsImage` l. 1050-1053, `ArchiveService.GetNumberOfPagesFromArchive` l. 79). MuPDF de KOReader ne lit que sa liste (`source/cbz/mucbz.c` l. 30-52 : ni AVIF ni JXL) plus `.webp` ajouté par `thirdparty/mupdf/webp-upstream-697749.patch` de koreader-base. Un chapitre AVIF : compté par Kavita, **absent** du bloc pour KOReader ; toutes les pages suivantes sont décalées. Un chapitre JXL : absent des deux côtés mais présent dans l'export.
- Ordre : Shelfhold exporte « les pages telles que reçues, dans leur ordre » d'archive (`infrastructure/packaging/cbz/packager.py` l. 35-36). Kavita, lui, aplatit les sous-dossiers en renommant (`DirectoryService.FlattenDirectory` l. 1132-1156) puis trie par **nom sans extension**, en tri naturel **dépendant de la culture** du serveur (`CacheService.GetCachedPages` l. 32-37, `EnumerableExtensions.OrderByNatural` l. 21-29, `StringComparer.CurrentCulture`). L'ordre « de Kavita » n'est reproductible en Python que pour des noms canoniques.
- Amendement : définir `page_map_state = verified` **seulement si** (1) tous les formats du membre sont dans l'intersection MuPDF ∩ Kavita (JPEG, PNG, WebP, GIF), (2) les noms sont canoniques (ASCII, sans sous-dossier, chiffres de même largeur, radicaux uniques), (3) le nombre de pages égale `Pages` de Kavita pour ce fichier ; sinon `unverified`, traduction au niveau du chapitre. Un membre AVIF ou JXL est **exclu** du bloc (et la série le signale) ou transcodé — mais un transcodage casse « virtuel, zéro octet ». Aujourd'hui l'ADR ne dit pas comment un membre devient `verified`. Les pages non-image (`ComicInfo.xml` de chaque chapitre, dernière entrée de nos CBZ) doivent être omises du bloc, ou rangées sous un nom non image : MuPDF les ignore déjà, mais la règle doit être écrite.

**MAJ-2. « Dernière » ou « plus avancée » : l'ADR se contredit, et la projection « antérieurs lus » est irréversible.**
- §5 : « la position renvoyée à KOReader est la dernière, pas la plus avancée » ; « Sens Kavita → bloc » : « on prend la position la plus avancée parmi les chapitres du bloc (règle de L14a) ». Il faut une seule règle, et un test qui la fixe (relire le chapitre 3 après le 40, fermer, rouvrir).
- Un saut dans le bloc (table des matières, aperçu de la fin, `GotoPage` erroné) suivi d'un tour de page pousse « chapitres 1 à N-1 lus » chez Kavita ; L14a (« jamais de retour à non lu ») et L14b (« jamais en arrière ») rendent l'erreur définitive, pour un bloc de 200 chapitres. Il faut un garde-fou : projection seulement si le saut est contigu à la position précédente connue, ou après une seconde position dans le même chapitre ; journal des projections et annulation par le membre ou l'admin.

**MAJ-3. `device` et `device_id` de la réponse ne sont pas spécifiés, or KOReader ignore une position qui semble venir de lui.**
- `main.lua` l. 861-869 : si `body.device == Device.model` et `body.device_id == self.device_id`, KOReader ne bouge pas (« Latest progress is coming from this device »). Si Shelfhold renvoie les champs du dernier `PUT` de cette liseuse alors que la position a été avancée dans Kavita Web, la lecture faite ailleurs est ignorée. La réponse doit porter `device = "Kavita"` (et un `device_id` propre) quand la position retenue vient d'un tirage Kavita.
- Horloges : à l'ouverture (non interactive), KOReader compare `timestamp` du serveur à `last_page_turn_timestamp = os.time()` de la liseuse (`main.lua` l. 897-903, l. 982). L'ADR ne traite que la dérive Shelfhold ↔ Kavita ; une liseuse en avance (Kobo sans NTP, batterie vidée) ignore en silence les positions plus récentes. À documenter, et à montrer sur l'écran « Connecter un appareil » (heure du serveur).

**MAJ-4. Rétention de `bundle_version`/`bundle_member` : illimitée, en conflit avec l'effacement immédiat.**
- « Les lignes d'une ancienne version restent » sans durée, alors que chaque nouveau chapitre d'une série suivie, chaque changement d'équipe, chaque profil (`visible_set_digest`) et surtout chaque **tranche « à partir du premier non lu »** (qui change à chaque chapitre lu) crée une version. Croissance sans borne, et tranches dont l'empreinte change à chaque lecture.
- ADR-0048 : un `content_hash` purgé n'existe plus qu'en AEAD dans `content_tombstone`. `bundle_member.content_hash` en clair, conservé « pour traduire », le contredit. Demandé : versions créées au **téléchargement** seulement (pas au rendu du flux) ; rétention = N jours après le dernier accès kosync, purge en cascade par `purge --now`, membres purgés remplacés par une ligne sans `content_hash` (position `orphan`) ; `replica` (EF-BIB-045) signale « copies livrées à des appareils, hors de portée » dans le rapport de purge. Les tranches « premier non lu » sortent de la première livraison.

**MAJ-5. L'URL versionnée doit revérifier la visibilité, et le flux doit être lié au contenu servi.**
- L'URL `…/bundle/{version_digest}.cbz` ne doit pas être une capacité : à chaque requête, **chaque membre** de la version doit être visible au profil du jeton **maintenant** (un chapitre reclassé au-dessus du plafond après la création de la version, ou une version calculée pour un autre profil dont l'empreinte a fuité dans un historique partagé). Test `test_bundle_version_url_rechecks_every_member_for_token_profile` (IDOR).
- Intégrité du flux : les tailles et CRC viennent du répertoire central des sources, mais les descripteurs sont ouverts « pour la durée du flux » sans dire quand. Si les 200 fichiers sont ouverts au fil de l'eau, une réécriture d'export pendant le flux mêle deux contenus sous des en-têtes valides (MuPDF ne vérifie pas le CRC, voir plus haut : rien ne le détectera). Ouvrir et vérifier (`content_hash` ou au moins taille + CRC du répertoire central) **tous** les membres avant le premier octet, sinon `410`. Un membre exporté en PDF ou en EPUB (`export/application/rendering.py` produit CBZ et EPUB, `pdf_store.py` des PDF) n'a pas de « répertoire central de CBZ » : la règle doit dire « membres CBZ seulement », et ce que devient une série mixte.

**MAJ-6. Reprise et grands fichiers : la conception suppose des capacités que KOReader n'a pas.**
- `OPDSBrowser:downloadFile` (l. 1513-1548) ne pose aucun `Range`, écrit en `"w"` et **supprime** le fichier sur tout code autre que 200 (l. 1541, 1547) : un téléchargement coupé recommence à zéro, et écraser V1 par V2 détruit V1 avant que V2 soit complet (le `.sdr` reste, voir BLQ-1). `Range`, `If-Range` et `206` ne servent à aucun client cible : à sortir de L49 (ou à garder minimal, sans la règle « `If-Range` ancien → version courante entière » qui sert sous l'URL de V1 les octets de V2).
- Le délai total de 60 s (`socketutil.lua` l. 32-33) n'est, d'après son propre commentaire (l. 40-48), presque jamais atteint pendant un transfert ; reste le délai de bloc de 15 s. Mais le téléchargement est synchrone dans l'interface : 2 Gio en Wi-Fi de liseuse, c'est 10 à 30 min d'écran figé, puis tout perdu à la moindre coupure. Le plafond par défaut devrait être bien plus bas (256 ou 512 Mio, parties « 1/N ») ; le « 2 Gio » reste un maximum réglable. Les parties doivent être coupées sur des frontières stables (par plage de chapitres fixée, pas par taille cumulée depuis le début), sinon un chapitre remplacé au début décale toutes les parties.

### Mineurs
- **MIN-1. Zip64 et nombre d'entrées.** « Sans Zip64 » sous 2 Gio est faux au-delà de 65 535 entrées (un webtoon de 1 000 chapitres de 70 bandes). MuPDF le lit : écrire la règle déterministe (Zip64 dès que `entrées > 65 535` ou `offset ≥ 2³²`) et la tester. Garder la taille strictement sous 2³¹ octets pour les liseuses 32 bits.
- **MIN-2. Table des matières gratuite.** KOReader construit un sommaire à partir des dossiers du CBZ (`thirdparty/mupdf/mupdf_cbz_chapter_support.patch`, `cbz_load_outline`). Avec `{rang}/{page}`, le sommaire affiche « 00001 », « 00002 ». Un nom de dossier `{rang} {numéro de chapitre}` (ASCII, chapitre visible seulement) donne un vrai sommaire sans risque d'ordre, puisque le rang en tête fixe le tri naturel (`cbz_strnatcmp`, `mucbz.c` l. 82-115).
- **MIN-3. Arrondi du pourcentage.** KOReader compare `Math.roundPercent` (plancher au 1/10 000, `frontend/optmath.lua` l. 18-20) et abandonne si égal (`main.lua` l. 877) ; au-delà de 10 000 pages, deux pages voisines se confondent. Sans gravité, à documenter dans les vecteurs de test.
- **MIN-4. Limitation de débit derrière un proxy.** « 5 par minute par couple (adresse, identifiant) » derrière un mandataire inverse sans liste de proxys de confiance : toutes les adresses sont celles du proxy, et quiconque connaît l'identifiant public bloque la liseuse du porteur. Ajouter un plafond par adresse seule et la règle `X-Forwarded-For` de confiance ; le recul exponentiel ne doit pas pénaliser un jeton qui réussit.
- **MIN-5. Déni de service.** « 2 flux par instance, 1 par jeton » : deux liseuses lentes (ou un jeton volé et le jeton d'un enfant) occupent toute l'instance pendant 30 min. Ajouter un débit minimal (coupure si moins de X Kio/s sur 60 s) et un `Retry-After` honnête ; calculer `koreader_partial_md5` et la taille une fois par version (pas à chaque rendu du flux), sinon le flux d'une étagère de 50 séries ouvre 50 × N répertoires centraux par requête.
- **MIN-6. Mot de passe kosync.** Le stockage `HMAC-SHA-256(clé serveur, md5(secret))` est sûr mais ajoute une clé à gérer (rotation ⇒ tous les jetons morts) ; avec 128 bits aléatoires, un SHA-256 simple suffit. Le secret est saisi deux fois sur clavier de liseuse (26 caractères) et stocké en clair dans les réglages OPDS de KOReader, `md5(secret)` dans ceux de kosync : l'écran doit le dire, et la révocation est la seule parade à la perte de l'appareil. Le passage EPUB (L51) envoie la clé Kavita du membre **dans l'URL** de Kavita (`KoreaderController.cs` l. 22-58), ce que l'ADR refuse pour lui-même : à signaler au moins.
- **m7. Un seul serveur kosync par liseuse.** KOReader n'a qu'un réglage de serveur : en pointant vers Shelfhold, tous les chapitres pris sur l'OPDS de Kavita passent aussi par le relais (prévu par `document_alias.kavita_md5`), et Shelfhold devient un point unique de panne pour toute la synchronisation. La fonction « synchro OPDS » de KOReader (`opdsbrowser.lua` l. 1877-1935, téléchargement des entrées nouvelles depuis `last_download`) permettrait, avec un flux « Nouvelles versions » trié du plus récent, de récupérer seule une nouvelle version ; à considérer avec BLQ-1 (nouveau chemin à chaque version).

### Découpage des lots
- **Premier incrément utile, plus tôt : oui.** L46 + L48 + L49 (sans tranches, sans `Range`) donnent au porteur le bloc d'un seul téléchargement ; la progression reste locale à la liseuse. L47 seul (kosync local) synchronise déjà plusieurs liseuses entre elles. L50 (relais) vient ensuite, quand BLQ-1, MAJ-1 et MAJ-2 sont tranchés. Ordre proposé : L46 → L48 → L49a (bloc, `bundle_version`, `410`) → L47 → L50 → L49b (tranches, parties) → L51.
- **L48** : le téléchargement d'un chapitre seul double l'OPDS de Kavita ; le retirer allège le lot (navigation et « Série complète » seulement).
- **L49** est sous-estimé à 8 h (écrivain zip déterministe et Zip64, empreinte, répertoires centraux, autorisation par version, sémaphores, `410`, tranches, parties) : le couper en L49a et L49b comme ci-dessus.
- **L50** doit dépendre aussi de L35 (préférence d'équipe, qui crée des versions) et de L15 (I30, transposition `approximate`) si I30 y est défini ; à vérifier dans PLAN-M4.
- **L51 n'est pas 💪** : il étend la liste fermée des routes Kavita du jeton d'un membre (L12, D8, secrets) ; la partie EPUB est 🔒. L'écran « Connecter un appareil » est nécessaire dès L46 (émission, affichage unique du secret, méthode binaire, TLS) : le rattacher à L46, ou fournir la CLI d'émission dans L46 et l'écran dans un lot 🆓 séparé.
- **🔒 de L46 à L50 : justes.** L49 l'est par l'autorisation de l'URL versionnée (MAJ-5) plus que par le déterminisme.

## Exigences liées
EF-LEC-009, EF-LEC-014, EF-LEC-007, EF-INT-007, EF-UX-004, EF-USR-006, EF-USR-022, EF-USR-024, EF-EXP-019, EF-EXP-026, ENF-QUAL-005, ENF-SEC-010.

## Sources
- https://github.com/koreader/koreader/blob/457a968/plugins/kosync.koplugin/KOSyncClient.lua
- https://github.com/koreader/koreader/blob/457a968/plugins/kosync.koplugin/main.lua
- https://github.com/koreader/koreader/blob/457a968/plugins/kosync.koplugin/api.json
- https://github.com/koreader/koreader/blob/457a968/frontend/apps/reader/modules/readerpaging.lua
- https://github.com/Kareadita/Kavita/blob/e844b64/Kavita.Server/Controllers/KoreaderController.cs
- https://github.com/Kareadita/Kavita/blob/e844b64/Kavita.Services/KoreaderService.cs
- https://github.com/Kareadita/Kavita/blob/e844b64/Kavita.Services/Helpers/KoreaderHelper.cs
- https://github.com/Kareadita/Kavita/blob/e844b64/Kavita.Server/Middleware/AuthKeyAuthenticationHandler.cs
- https://github.com/Kareadita/Kavita/blob/e844b64/Kavita.Models/DTOs/Koreader/KoreaderBookDto.cs
- `docs/decisions/M4.md` (L13), ADR-0045, ADR-0049, ADR-0052
- `~/shelfhold-spec/PLAN-M4.md` (L10, L12, L13, L14a, L14b, §6 D8 et D11)
