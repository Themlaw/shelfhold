---
status: accepté
date: 2026-11-02
amended: 2026-11-03 (passe 5) ; 2026-11-03 (passe 6) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 11)
decision-makers: mainteneur
---
# ADR-0028 — Mode passage et disposition du stockage (`/data`, `/store`, `/library`)

> Amende ADR-0010 (stockage par Unité) et complète ADR-0022 (montage) et ADR-0023 (sauvegarde).

## Contexte et problème
Après la passe 1, chaque Unité était stockée dans `/data/store` puis **copiée** dans la bibliothèque Kavita, alors que Suwayomi garde déjà son propre téléchargement. Sur le profil de référence (2 To), le contenu existait jusqu'à **trois fois** (GAP-2-10). Par ailleurs :
- la base SQLite et les fichiers partageaient le même volume, ce qui force un NAS à choisir entre un SSD trop petit et des disques durs qui ne se mettent jamais en veille (N5) ;
- le magasin n'était ni localisé ni sauvegardé, et le RTO d'une heure était irréaliste s'il incluait 2 To (GAP-2-09) ;
- rien ne distinguait un fichier dont la source a disparu, qui est le dernier exemplaire (N6) ;
- toute retouche de métadonnées réécrivait le fichier exporté et faisait perdre la position KOReader (GAP-2-14) ;
- la réinstallation de Kavita cassait les liaisons fondées sur ses identifiants (GAP-2-13).

## Facteurs de décision
- Un homelab a un disque fini : pas de copie sans usage.
- Garder un exemplaire quand il est **irremplaçable**.
- Séparer données chaudes (base) et froides (fichiers) **avant** la 1.0, car changer de disposition ensuite impose une migration à chaque utilisateur.
- Respecter EXDEV : liens durs seulement sur le même système de fichiers et le même montage.

## Options considérées
### A. Copie conservée partout (situation de la passe 1)
- Avantages : réexport toujours possible depuis Shelfhold ; vérification d'intégrité locale.
- Inconvénients : ×2 à ×3 sur le disque ; sauvegarde du magasin irréaliste ; magasin et base sur le même volume.
### B. Mode passage par défaut pour les services connectés, copie conservée en option par étagère, volumes `/data` et `/store` séparés
- Avantages : une copie au plus par Shelfhold ; la source amont (Suwayomi) reste le moyen de re-télécharger ; `/data` petit et sauvegardable ; `/store` sur disques durs, même système de fichiers que `/library` pour les liens durs.
- Inconvénients : un fichier manquant dans la bibliothèque se répare par re-téléchargement, pas localement ; deux volumes à monter.
### C. Liens durs systématiques magasin ↔ bibliothèque
- Avantages : aucune copie.
- Inconvénients : impossible dès que le ComicInfo est injecté (octets différents) ; impose le même montage ; une modification en place casserait les deux.

## Décision
Option B (arbitrages B6, B10–B14).
- **Mode passage** (EF-BIB-027) : défaut des services connectés ; le CBZ récupéré est construit en staging (ComicInfo en dernière entrée) puis renommé dans `/library` ; Shelfhold garde `unit.json`, hash et manifeste, **pas de troisième copie**. Option « purger le téléchargement Suwayomi après export réussi » (API Suwayomi), **désactivée par défaut**.
- **Copie conservée** : option par étagère (défaut pour les sources natives et le dossier d'import) dans `/store`.
- **Volumes** (ENF-OPS-012) : `/data` (SQLite, config, `unit.json`, vignettes ; SSD conseillé, local) et `/store` (copies ; disque dur possible, même système de fichiers que `/library`).
- **Sauvegarde** (EF-INT-015) : `/data` + manifestes ; `/store` documenté comme à sauvegarder par l'outil de l'hôte ; RTO 1 h = remise en service ; contenu restauré par ré-adoption de Kavita et re-téléchargement.
- **Liens Kavita par chemin relatif** (B11, EF-EXP-039) ; identifiants Kavita en cache ré-résolu ; empreinte d'instance.
- **Pas de réécriture** d'un fichier exporté pour une simple modification de métadonnées (B12, EF-EXP-040) ; API Kavita quand c'est possible ; action explicite « Réécrire » avec avertissement KOReader.
- **Irremplaçable** (B13, EF-BIB-031) : drapeau calculé ; suppression impossible sans confirmation explicite (invariant testé) ; manifeste BagIt en M4 ; PAR2 jamais.
- **Fenêtre de maintenance** (B14, ENF-OPS-014) : jobs touchant `/store` et `/library` regroupés pour laisser dormir les disques.
- **Adoption** : fichiers référencés, non copiés ; option « intégrer au magasin ».

## Conséquences
- Positives : budget disque ×1 à ×2 au lieu de ×3 ; sauvegarde réaliste ; veille des disques possible ; liaisons qui survivent à une réinstallation de Kavita ; progression KOReader préservée.
- Négatives : en mode passage, un fichier abîmé ou supprimé dans la bibliothèque se répare par re-téléchargement (impossible si la source a disparu : d'où le drapeau « irremplaçable » et la suggestion « conserver une copie ») ; deux volumes dans le compose.

## Risque principal
Source disparue alors que l'étagère est en mode passage et que le fichier de la bibliothèque est perdu. Mitigations : drapeau `irreplaceable` recalculé, suggestion de copie conservée, page Santé, doc 3-2-1 pour `/library` et `/store`.

## Réversibilité
Haute pour le mode (réglage par étagère ; passer en copie conservée remplit `/store` par un job) ; moyenne pour la disposition des volumes (fixée en M0 justement pour éviter une migration).

## Amendement passe 3 (2026-11-02)
- **Mettre à l'abri** (C6, EF-BIB-038) : en mode passage avec purge, le dernier exemplaire pouvait n'exister que dans `/library`, hors sauvegarde, et le drapeau `irreplaceable` arrivait trop tard. Désormais, avant toute purge Suwayomi ou en mode passage, si la source est fragile ou disparue, ou si l'Unité est candidate « irremplaçable » (dès **2 échecs consécutifs** de la source), un job copie le fichier dans `/store` (lien dur si possible ; **remplacé passe 4 : copie indépendante, jamais de lien dur**, voir l'amendement passe 4) ; la purge n'est permise que si (copie Kavita) ∧ (copie `/store` ∨ source vivante). Page Santé : « irremplaçables sans copie conservée ».
- **Formulation O7** (GAP-3-22) : jamais trois exemplaires en mode passage ; ×3 possible en copie conservée sans lien dur, signalé.
- **Fenêtre de maintenance** (GAP-3-23) : seuls les **parcours** (vérification, intégrité, scans de masse) y sont confinés ; l'acquisition et le rangement déclenchés par un événement restent permis à toute heure.

## Amendement passe 4 (2026-11-03)
(D8, D9, GAP-4-06, GAP-4-07) : (1) une copie conservée ou une mise à l'abri est une **copie indépendante** (reflink si possible), **jamais un lien dur**, car un lien dur partage l'inode : une modification sur place, une troncature ou une corruption toucherait les deux noms ; le lien dur reste une option (M2, EF-EXP-015) pour le contenu remplaçable seulement ; la page Santé ne compte que les copies indépendantes ; (2) les échecs sont comptés **par source amont**, seules les **erreurs de contenu** comptent pour « irremplaçable », et la mise à l'abri est soumise à un **quota** (espace libre de `/store` moins la réserve, lots plafonnés, `unit.safeguard_backlog`) — règle unique EF-BIB-042 ; (3) la modification sur place est détectée par la taille et le mtime (EF-EXP-043).

## Amendement passe 5 (2026-11-03)
E7, GAP-5-22. `unit.json` vit **toujours dans `/data`** (`/data/meta/…`), dans tous les modes, jamais dans `/library` ni `/store` ; formats versionnés selon ADR-0039. Passer une étagère du mode passage à la copie conservée propose un job de rattrapage soumis au quota d'EF-BIB-042. En mode multi-comptes de Suwayomi, la purge ne libère de l'espace que si aucun autre compte ne garde le fichier : l'espace libéré est mesuré (EF-SRC-031).

## Amendement passe 6 (2026-11-03)
F4 (GAP-6-04).
- En mode passage, `replica` (EF-BIB-045) enregistre les exemplaires réels (amont, `/library`, `adopted_ro`) ; `independent_copies(unit)` décide de la purge et de la mise à l'abri, qui copie depuis le fichier de `/library` **seulement si son hash correspond au manifeste**, sinon depuis l'amont.
- Une Œuvre **non exportée** n'a aucun octet local en mode passage : le lecteur intégré (M5) ne lit que des Œuvres en copie conservée (`01` §1.3 corrigé).
- `meta/` fait partie du jeu de sauvegarde de `/data` (ENF-OPS-020).

## Amendement passe 9 (2026-11-03)
J1, J2.
- Pour une source `untrusted` fragile, « Mettre à l'abri » garde l'**original** (staging ou source) et ne retombe sur le dérivé neutralisé (`extracted_derived`) qu'à défaut (ADR-0047).
- `purge --now` (ADR-0048) passe outre « Mettre à l'abri », la corbeille et I11 pour les copies gérées ; une Unité sous pierre tombale n'est jamais mise à l'abri.

## Amendement passe 10 (2026-11-03)
K9 (GAP-10-07).
- **Exception au mode passage** : l'original d'un EPUB `untrusted` est gardé dans `/store/untrusted/` même quand l'étagère est en mode passage ; sans lui, un contournement découvert dans le neutraliseur ne pourrait se corriger qu'en re-neutralisant un dérivé déjà réécrit. Ces originaux sont petits ; sans `/store` configuré, l'admin est averti à la liaison et la mise à jour de sécurité repart du dérivé rangé (mode dégradé signalé).

## Amendement passe 11 (2026-11-03)
GAP-11-13. Rédaction alignée : « en mode passage, `/store` ne contient aucun fichier » et « jamais trois exemplaires en mode passage » (O7) s'entendent **hors original d'un EPUB `untrusted`**, gardé dans `/store/untrusted/` pour corriger le neutraliseur (EF-BIB-051, EF-BIB-006) ; cet original n'est jamais servi à une destination (EF-BIB-045).

## Exigences liées
EF-BIB-006, EF-BIB-027, EF-BIB-030, EF-BIB-031, EF-SRC-009, EF-EXP-015, EF-EXP-018, EF-EXP-028, EF-EXP-039, EF-EXP-040, EF-INT-015, ENF-OPS-001, ENF-OPS-005, ENF-OPS-006, ENF-OPS-012, ENF-OPS-013, ENF-OPS-014, EF-BIB-038, EF-BIB-042, EF-EXP-043, EF-BIB-006, EF-BIB-027, EF-SRC-031, EF-BIB-045, ENF-OPS-020, EF-BIB-052, EF-BIB-051.

## Sources
- https://immich.app/docs/administration/backup-and-restore (sauvegarder le dossier de téléversement séparément de la base)
- https://wiki.servarr.com/sonarr/settings#media-management (liens durs, dossiers vides, épisodes supprimés)
- https://forums.unraid.net/topic/197420-spindown-nightmare/ · https://forums.unraid.net/topic/150676-disk-1-never-goes-to-sleep-fileactivity-shows-nothing
- https://www.rfc-editor.org/rfc/rfc8493 (BagIt)
- `deliverables/review/chasseur-trous-passe2.md` (GAP-2-08, 09, 10, 13, 14), `deliverables/review/explorateur-passe2.md` (N5, N6), `deliverables/review/ARBITRAGES-passe2.md` (B6, B10–B14)
- `deliverables/review/chasseur-trous-passe3.md` (GAP-3-06, GAP-3-22, GAP-3-23)
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-06, GAP-4-07)
- https://man7.org/linux/man-pages/man2/rename.2.html
- `deliverables/review/ARBITRAGES-passe4.md`
- `deliverables/review/chasseur-trous-passe5.md`
- `deliverables/review/explorateur-passe5.md`
- `deliverables/review/ARBITRAGES-passe5.md`
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)
