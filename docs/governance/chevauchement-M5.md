# Revue de chevauchement : sortie de M5 (1.2)

Source : ENF-GOV-003, ENF-GOV-002 ; suite des revues de [M0](chevauchement-M0.md) et de [M3](chevauchement-M3.md). Faite le 2026-11-20 **sans aucune requête réseau** : elle relit ce que le dépôt cite déjà (la revue de M3 du 2026-11-15, les ADR-0020, ADR-0053 et ADR-0055, la grille de couverture 05) et confronte ce que M5 a livré à ce que ces sources disent des trois projets. **Ce n'est pas une lecture neuve des notes de version** : tout ce qui a paru depuis le 2026-11-15 est inconnu de cette revue, et la décision ci-dessous le dit. Aucune version n'a été installée ni essayée. M4 n'avait pas de fichier propre (sa relecture du registre de pré-mortem est datée du 2026-11-18) : cette revue couvre M4 et M5 depuis le 2026-11-15.

## Sources

| Projet | Lu (dans le dépôt) | Lien |
|---|---|---|
| Kavita | revue de M3 (aucune version publiée depuis v0.9.1.4, 2026-09-02 ; commits avec la synchronisation Kobo en correction) ; ADR-0020 (axe Kobo annoncé pour 2026) ; ADR-0053 (routes kosync de Kavita relayées) | <https://github.com/Kareadita/Kavita> |
| Komga | revue de M3 (1.30.1, 2026-11-10) ; grille 05 (synchronisation Kobo, clé par appareil dans le chemin) ; revue de M0 (OAuth2 de l'interface « NextUI ») | <https://github.com/gotson/komga/releases> |
| Suwayomi-Server | revue de M3 (v2.4.2366, 2026-09-30, version épinglée du banc de contrat ; flux OPDS en évolution) | <https://github.com/Suwayomi/Suwayomi-Server> |

## Constats

| Projet | Fonction proche de M5 | Effet sur le périmètre de Shelfhold |
|---|---|---|
| Kavita | Lecteur web complet (le lieu de lecture de la famille) ; kosync et OPDS propres ; synchronisation Kobo en cours de correction dans ses commits. | **Aucune exigence retirée.** Le lecteur de Shelfhold est volontairement borné (D-P1, option 3 : contenu non exporté et « Lire un extrait » ; un livre exporté vers une destination lisible se lit là-bas, ADR-0055), donc il ne fait pas concurrence frontale. Le relais kosync vers Kavita (M4, L09 de M5) ne reprend que le pourcentage pour un EPUB. La synchronisation Kobo reste le seul recoupement ouvert : voir Q21 ci-dessous. |
| Komga | Synchronisation Kobo (clé par appareil), OPDS, connexion OAuth2 (interface « NextUI ») ; sert de destination à Shelfhold. | **Aucune.** L'OIDC de Shelfhold (EF-USR-004) protège l'atelier, pas la bibliothèque de Komga ; il ne lie qu'à un compte existant. Komga reste une destination (M4) ; le relais de scan et l'audit des comptes passent en M6, sans lien avec cette revue. |
| Suwayomi | Flux OPDS de ses chapitres, comptes (« UI Login »). | **Aucune.** Le flux OPDS de Shelfhold (1.2, PSE, 2.0) est filtré par profil et par jeton d'appareil, ce que Suwayomi ne fait pas d'après les sources lues ; Suwayomi reste la source, non le lieu de lecture. |

## Q21 : la synchronisation Kobo (EF-INT-012)

Révisée à la sortie de M5, comme 06 le demande. **Établi** : Kavita a annoncé l'axe (ADR-0020), et ses commits du 2026-11-15 contenaient des corrections de la synchronisation Kobo sans version publiée (revue de M3). **Non établi** : si une version de Kavita publiée depuis le 2026-11-15 la livre, et si elle respecte le contrôle de profil (plafond d'âge, étagères réservées) que Shelfhold impose. Détail et décision dans `docs/decisions/M5.md` (« Révision de Q21 ») : Q21 reste « laissée à Kavita », EF-INT-012 reste conditionnelle et **non planifiée** en M6, à relire dans les notes de version de Kavita **à l'entrée de M6**.

## Ce qui ne bouge pas, ce qui est à suivre

- **Rien n'est livré par un tiers à la place d'une exigence de M5.** Aucune exigence ne passe en « Jamais » à ce stade (risque R24 du [registre de pré-mortem](pre-mortem.md) inchangé).
- **À lire avant de tagger** (par le porteur, avec les essais sur appareil de D-P3) : les notes de version de Kavita, Komga et Suwayomi parues depuis le 2026-11-15, en cherchant la synchronisation Kobo, les routes kosync et le flux OPDS.
- **À relire à la prochaine version épinglée de Suwayomi** : la sonde d'EF-SRC-029 et le flux OPDS (suite de la revue de M3).
- Ni un nom de site de téléchargement ni un outil de contournement n'est cité ici, et la revue n'en recommande aucun.

## Relecture du registre de pré-mortem

[pre-mortem.md](pre-mortem.md) relu le 2026-11-20. Ligne 3 (« Kavita, Komga ou Suwayomi livrent la fonction ») : signal non déclenché **d'après les sources du dépôt**, avec la réserve de Q21. Une ligne est ajoutée (9, contenu non fiable du lecteur). Les autres lignes n'ont pas de signal nouveau, hormis la vélocité, relevée dans [velocite.md](velocite.md).

## Décision

- Aucune exigence n'est déclarée livrée par un tiers : aucune passe en « Jamais ».
- Q21 : voir ci-dessus ; **à confirmer par le porteur**.
- Revue **sans réseau** : à compléter par la lecture des notes de version avant la release, ou à l'entrée de M6 au plus tard.
- Prochaine revue : sortie de M6, ou à l'entrée de M6 pour Q21.
