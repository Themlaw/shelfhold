# Revue de chevauchement : sortie de M6 (1.3)

Source : ENF-GOV-003, ENF-GOV-002 ; suite des revues de [M0](chevauchement-M0.md), de [M3](chevauchement-M3.md) et de [M5](chevauchement-M5.md). Faite le 2026-11-24 **avec une lecture en ligne** des pages de versions publiées sur GitHub (contrairement à la revue de M5, faite sans réseau). Les dates des pages de versions s'affichent sans année : elles sont lues comme 2026, ce que recoupent les revues précédentes (v0.9.1.4 de Kavita au 2026-09-02, Komga 1.30.1 au 2026-11-10, Suwayomi v2.4.2366 au 2026-09-30). Aucune version n'a été installée ni essayée ; ce qui n'est pas lisible sur ces pages est marqué « non vérifié ».

## Sources

| Projet | Versions lues (notes de version) | Lien |
|---|---|---|
| Kavita | v0.9.1.4 (02 sept., « Hardcover Recommendations + Hotfix »), dernière version publiée ; v0.9.1 (28 août, « Kavita+ Overhaul and 100K Installs ») ; v0.9.0.2 (14 mai) ; les notes de v0.9.1 annoncent la v0.9.2 « Native Kobo Sync Support ». Page du wiki « Kobo Sync » (branche de développement) | <https://github.com/Kareadita/Kavita/releases/tag/v0.9.1.4>, <https://github.com/Kareadita/Kavita/releases/tag/v0.9.1>, <https://wiki.kavitareader.com/guides/features/kobo-sync/> |
| Komga | 1.30.2 (24 nov., correctifs : cookie de session, liaison de la source de données, collateurs ICU) ; 1.30.1 (10 nov.) ; 1.30.0 (04 nov.) ; 1.29.2 (28 oct.) ; 1.29.0 (12 oct.) | <https://github.com/gotson/komga/releases/tag/1.30.2>, <https://github.com/gotson/komga/releases/tag/1.30.0> |
| Suwayomi-Server | v2.4.2373 (21 nov., correctifs de sécurité) ; v2.4.2372 (21 nov.) ; v2.4.2366 (30 sept., version épinglée du banc de contrat) ; v2.3.2243 (13 juil.) | <https://github.com/Suwayomi/Suwayomi-Server/releases/tag/v2.4.2373>, <https://github.com/Suwayomi/Suwayomi-Server/releases/tag/v2.4.2366> |
| Calibre-Web Automated | v4.0.8 (28 sept., « Kobo First Sync Fixed ») ; v4.0.7 (27 sept.) ; v4.0.3 à v4.0.6 (31 janv. au 04 févr.) | <https://github.com/crocodilestick/Calibre-Web-Automated/releases/tag/v4.0.8> |

Kapowarr et Readarr n'ont pas été relus : un seul concurrent de gestion de bibliothèque suffit à cette revue, et leur état de maintenance n'a pas été vérifié (non vérifié).

## Constats

| Projet | Fonction proche de M6 | Effet sur le périmètre de Shelfhold |
|---|---|---|
| Kavita | **Synchronisation Kobo : pas dans une version stable publiée.** Aucune version publiée après v0.9.1.4 (2026-09-02) ; la v0.9.2 est annoncée, non publiée. Le wiki (branche de développement) la décrit : activation par l'administrateur, réglages par utilisateur, filtre par bibliothèques, listes de lecture ou collections, conversion en KEPUB par un outil intégré, progression EPUB, annotations dans les deux sens, point de synchronisation par appareil. **Plafond d'âge : non mentionné** dans ce que le wiki expose ; non vérifié. **Trackers : livrés** depuis v0.9.1 (progression, notes et critiques envoyées à AniList, MAL, MangaBaka et Hardcover, par OAuth). **Plugins : aucun système.** Les notes de v0.8.9 citent seulement des applications tierces qui appellent l'API (un greffon KOReader, une application Android). | Synchronisation Kobo : voir D-P5 ci-dessous. Trackers : recoupement partiel (voir tableau suivant). Plugins : rien, Shelfhold se différencie. |
| Komga | 1.29.0 à 1.30.2 : OAuth2 et interface « NextUI », correctifs KOReader (hachage, format de ressource unique, retrait de la gestion de session de kosync). **Aucune version lue ne mentionne Kobo, plugin ou tracker.** La synchronisation Kobo de Komga citée dans la grille 05 n'apparaît pas dans ces six versions : non vérifié si elle est plus ancienne ou si la page l'omet. | Aucune exigence retirée. Komga reste une destination (M4). |
| Suwayomi | v2.4.2366 : correctif d'installation d'extension, migration du dépôt d'extensions, correctif du tracker Shikimori. v2.3.2243 : installation d'une extension depuis un jar, API d'extension v1.6, support de SyncYomi. v2.4.2372 et v2.4.2373 : correctifs de sécurité. Un système d'extensions existe donc, mais c'est celui de la **source** (extensions de sources). Trackers : Shikimori nommé ; AniList, MAL et Kitsu non cités dans les six versions lues (non vérifié ailleurs). | Les extensions de Suwayomi sont des modules de source ; elles ne sont pas des plugins de bibliothèque isolés, signés et versionnés. Aucune exigence retirée. La version épinglée du banc de contrat reste v2.4.2366 ; les versions de sécurité du 2026-11-21 sont à relire avant de la faire avancer. |
| Calibre-Web Automated | Synchronisation Kobo livrée et corrigée en continu (v4.0.3 à v4.0.8) : proxy du store par défaut avec points d'accès OAuth de repli, conversion en KEPUB au téléchargement (v4.0.7), étagères « magiques » synchronisées (v4.0.3), prise en charge du micrologiciel 4.45+. Greffon KOReader et envoi de la progression à Hardcover (v4.0.7). Pas de système de plugins tiers lu dans ces notes. Filtre par âge ou par profil : non mentionné (non vérifié). | Recoupe l'émulation Kobo et le dossier KEPUB de M6, mais pour une bibliothèque Calibre, pas pour une bibliothèque Kavita avec contrôle de profil. |

## Fonction par fonction de M6

| Fonction de M6 | Livrée ailleurs ? | Décision |
|---|---|---|
| Plugins tiers isolés dans un processus compagnon | Non, dans aucun des quatre projets lus. Suwayomi a des extensions de source, sans l'isolation ni la signature. | **Garder, différencier.** |
| Index de plugins signé (minisign) | Non. | **Garder.** |
| SDK 2.0 pour les auteurs | Non. L'API publique de Kavita et l'API d'extension de Suwayomi (v1.6) existent, sans kit de conformité ni compatibilité garantie lues. | **Garder.** |
| Cycle de vie (mise à jour, épinglage, retour arrière) | Suwayomi gère la mise à jour d'extensions (et corrige les échecs de mise à jour en v2.4.2366) ; pas d'épinglage ni de retour arrière lus. | **Garder** ; l'épinglage et le retour arrière différencient. |
| Contrat Tracker (liaison de profil, progression poussée sur opt-in) | **En partie.** Kavita envoie progression, notes et critiques à AniList, MAL, MangaBaka et Hardcover depuis v0.9.1 (v0.9.1.4 ajoute des recommandations Hardcover). Suwayomi a des trackers (Shikimori nommé). Calibre-Web Automated envoie la progression KOReader à Hardcover. | **Garder, mais ne pas rivaliser sur le nombre de services** : la différence est la liaison par profil et l'opt-in, avec un contrat ouvert aux plugins. Ne pas dupliquer un service que Kavita livre déjà sans raison de profil. |
| Émulation minimale de la synchronisation Kobo (initialisation, library/sync par pages ; ni état de lecture ni proxy du store) | **En partie.** Kavita : publiée nulle part en version stable (branche de développement seulement). Calibre-Web Automated : livrée, avec proxy du store et progression, pour Calibre. Komga : non lue (non vérifié). | **Garder** (L17a, cas (b) de D-P5) ; voir D-P5. |
| Dossier Kobo en KEPUB | Calibre-Web Automated convertit en KEPUB au téléchargement (v4.0.7) ; Kavita le fait via la synchronisation en développement. Aucun ne dépose un dossier KEPUB depuis une destination d'export de Shelfhold (non vérifié). | **Garder, différencier** : c'est une destination d'export, pas un serveur de synchronisation. |
| Quotas des caches dérivés | Rien lu. | **Garder.** |
| Écrans des modules et des webhooks, rotation du secret des webhooks | Rien lu. | **Garder.** |

## D-P5 : critère de reprise de la synchronisation Kobo complète

Rappel du critère (06, Q21 ; ADR-0056) : EF-INT-012 est tranchée quand **Kavita 0.9.2 est publiée** (page des versions publiées, pas la branche de développement) avec la synchronisation Kobo **filtrée par bibliothèque et par plafond d'âge**, **et** que le porteur juge le filtre par bibliothèque suffisant.

Vérifié au 2026-11-24 :

- **Publication** : non atteinte. La dernière version publiée de Kavita est v0.9.1.4 (2026-09-02), soit 83 jours sans nouvelle version ; la v0.9.2 n'apparaît pas sur la page des versions ([v0.9.1.4](https://github.com/Kareadita/Kavita/releases/tag/v0.9.1.4)). Les notes de [v0.9.1](https://github.com/Kareadita/Kavita/releases/tag/v0.9.1) l'annoncent seulement.
- **Filtre par bibliothèque** : décrit par le wiki (branche de développement), avec en plus les listes de lecture et les collections.
- **Plafond d'âge** : non vérifié, rien sur la page lue.
- **Avis du porteur** : aucune réponse enregistrée (`docs/decisions/M6.md`, L17a).

**Le critère n'est pas atteint au 2026-11-24.** L'émulation minimale de M6 (cas (b)) reste justifiée et n'est pas retirée ; EF-INT-012 ne passe pas en « Jamais ». À relire à la publication de la v0.9.2 : si elle sort avec les deux filtres et que le porteur répond « la bibliothèque suffit », la surface Kobo de Shelfhold est à rouvrir pour retrait (L25) ; sinon elle reste, en gardant le contrôle de profil (étagère réservée, plafond d'âge, jeton par appareil) que Kavita ne documente pas.

## Ce qui ne bouge pas, ce qui est à suivre

- **Aucune exigence de M6 n'est livrée en entier par un tiers** : aucune ne passe en « Jamais » (risque R24 du [registre de pré-mortem](pre-mortem.md) inchangé). Deux recoupements partiels : les trackers (Kavita) et la synchronisation Kobo (Calibre-Web Automated, Kavita en développement).
- **À relire** : la page des versions de Kavita à chaque publication (v0.9.2 : filtres Kobo, plafond d'âge) ; Komga pour la synchronisation Kobo, que cette lecture n'a pas retrouvée ; la version épinglée de Suwayomi (correctifs de sécurité du 2026-11-21).
- **Limite** : lecture des pages de versions et d'une page de wiki, sans le code ni un essai. Les fonctions « non vérifié » ci-dessus ne sont pas déclarées absentes.
- Ni un nom de site de téléchargement ni un outil de contournement n'est cité ici, et la revue n'en recommande aucun. Un fichier protégé n'est jamais converti ni contourné par Shelfhold.

## Relecture du registre de pré-mortem

[pre-mortem.md](pre-mortem.md) relu le 2026-11-24. Ligne 3 (« Kavita, Komga ou Suwayomi livrent la fonction ») : signal **non déclenché** pour la synchronisation Kobo (Kavita : pas de version stable) ; signal **partiel** pour les trackers (Kavita, depuis v0.9.1), sans effet sur le contrat de Shelfhold. Les autres lignes n'ont pas de signal nouveau.

## Décision

- Aucune exigence n'est déclarée livrée par un tiers : aucune passe en « Jamais ».
- D-P5 : critère **non atteint** au 2026-11-24 ; EF-INT-012 reste construite en surface minimale (L17a) ; **à confirmer par le porteur**.
- Trackers : garder le contrat, sans multiplier les services.
- Prochaine revue : sortie de M7, ou à la publication de la v0.9.2 de Kavita.
