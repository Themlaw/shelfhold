---
status: accepté
date: 2026-11-02
amended: 2026-11-03 (passe 6)
decision-makers: mainteneur
---
# ADR-0033 — Fichiers tatoués : préserver, ne jamais diffuser hors du foyer

## Contexte et problème
Beaucoup d'EPUB achetés « sans mesure technique » en France sont **tatoués** : un filigrane identifie l'acheteur (nom, e-mail, numéro de commande), dans un fichier de l'archive, dans les métadonnées ou dans le texte. C'est précisément la voie d'achat légale que Shelfhold accepte (EF-SRC-012). Deux règles s'appliquent : le tatouage relève des **informations sur le régime des droits** (CPI L331-11) et **sa suppression est un délit** (CPI L335-3-2) ; il contient des **données personnelles**. Ce n'est pas une mesure technique de protection : la sonde de l'import ne s'en occupe pas. Or EF-EXP-033 autorise la réécriture de l'OPF ; un rezippage qui écarte les entrées inconnues, ou un paquet de diagnostic, pourrait retirer ou divulguer le tatouage (P6).

## Facteurs de décision
- Ne jamais retirer un tatouage, même par effet de bord.
- Ne jamais divulguer les données de l'acheteur (diagnostic, issue, fixture).
- Garder l'écriture OPF utile au rangement (série, index, âge).

## Options considérées
### A. Ignorer le tatouage (situation passe 2)
- Avantages : rien à faire.
- Inconvénients : retrait possible par rezippage ; divulgation par un paquet de diagnostic.
### B. Réécriture ciblée de l'OPF + copie octet pour octet du reste + drapeau `watermarked` + exclusions
- Avantages : préserve tout ce qui n'est pas l'OPF ; empêche la diffusion ; coût faible.
- Inconvénients : détection heuristique imparfaite (complétée par la saisie de l'utilisateur).
### C. Ne jamais toucher un EPUB acheté (aucune écriture OPF)
- Avantages : sûreté maximale.
- Inconvénients : perte du rangement en série et du complément d'âge pour les achats.

## Décision
Option B (arbitrage C19), qui vaut pour **tout** EPUB passthrough (le drapeau ajoute les exclusions).
- Réécriture **ciblée** de l'OPF dans l'archive ; toutes les autres entrées recopiées **octet pour octet** (`test_opf_edit_preserves_all_other_entries_bytewise`, invariant I14).
- Drapeau `watermarked` posé par détection heuristique ou par l'utilisateur à l'import (EF-BIB-041).
- Un fichier tatoué n'entre dans aucun paquet de diagnostic ou de support, aucune fixture, aucune issue, et n'est exporté vers aucune destination hors du foyer (ENF-LEG-009).
- Documentation : « un achat tatoué se lit dans le foyer, jamais au-delà ; Shelfhold ne retire pas le tatouage et ne le fera jamais ». Retirer un tatouage = **Jamais** (`01` §5.4).

## Conséquences
- Positives : conformité CPI ; vie privée de l'acheteur protégée ; rangement des achats conservé.
- Négatives : un module d'archive dédié (remplacement d'une entrée sans réécrire les autres) à maintenir.

## Risque principal
Un tatouage non détecté inclus dans un diagnostic. Mitigation : les paquets de diagnostic ne contiennent **jamais** de fichier de contenu, seulement hash et taille ; le drapeau n'ajoute qu'une seconde barrière.

## Réversibilité
Haute : règles locales, aucun format sur disque modifié.

## Amendement passe 6 (2026-11-03)
F3 (GAP-6-11).
- Mécanisme de « jamais hors du foyer » : chaque destination déclare `egress: foyer | externe` (EF-EXP-058) ; le planificateur **refuse** tout fichier `watermarked` vers une destination `externe` (envoi par e-mail vers un service de liseuse, synchronisation passant par un nuage tiers), **sans passe-droit** (F3 écarte la confirmation de l'admin proposée par GAP-6-11). Test dans le kit de conformité des destinations (M4) et, dès M1a, sur une fausse destination `externe`.

## Amendement passe 9 (2026-11-03)
J1 (X1).
- Un fichier `watermarked` n'est **jamais** neutralisé ni réécrit hors de l'OPF : un achat tatoué est `content_trust = publisher` par construction ; la frontière de confiance du contenu (ADR-0047) ne s'applique qu'aux EPUB `untrusted`.

## Exigences liées
EF-SRC-012, EF-EXP-033, EF-BIB-041, ENF-LEG-002, ENF-LEG-009, EF-EXP-058, EF-BIB-051.

## Sources
- https://www.legifrance.gouv.fr/codes/texte_lc/LEGITEXT000006069414/ (Code de la propriété intellectuelle, art. L331-11 et L335-3-2) *[mem]*
- `deliverables/review/explorateur-passe3.md` (P6), `deliverables/review/ARBITRAGES-passe3.md` (C19)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
