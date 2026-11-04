---
status: accepté
date: 2026-11-04
decision-makers: porteur, mainteneur
---
# ADR-0051 — Thèmes du front : quatre directions sur un socle unique, « Bibliothèque » par défaut

## Contexte et problème
Quatre directions de design ont été proposées (`front-propositions.html`) : A « Bibliothèque », B « Console », C « Galerie », D « Calme ». Le porteur les apprécie toutes et demande A par défaut, avec la possibilité de changer de thème (`review/ARBITRAGES-porteur2.md`, Q2). Le front est une SPA SvelteKit maintenue par une seule personne (ADR-0013) ; la recommandation de Q18 était une seule direction pour la 1.0. Il faut offrir le choix sans multiplier les écrans à maintenir ni casser l'accessibilité.

## Facteurs de décision
- Coût de maintenance pour un mainteneur seul (un écran = une implémentation).
- Accessibilité : contraste AA dans chaque thème, en clair et en sombre (ENF-A11Y-001).
- Budget de la 1.0 (M3, Q18) et règle de coupe des 150 % (ADR-0035).
- Aucune logique métier ne doit dépendre de l'apparence (tests, contrôle parental, i18n).

## Options considérées
### A. Une seule direction (recommandation initiale de Q18)
- Avantages : une seule surface à tester ; le plus simple.
- Inconvénients : écarte trois directions que le porteur veut garder ; aucune personnalisation par membre du foyer.
### B. Quatre fronts distincts (écrans propres à chaque direction)
- Avantages : chaque direction exprimée sans compromis (mise en page Console très dense, Galerie centrée sur les couvertures).
- Inconvénients : quatre fois les écrans, les tests et les corrections ; dérive certaine entre thèmes ; intenable pour un mainteneur seul.
### C. Socle unique + thèmes = jetons de design + variantes de mise en page déclarées (retenue)
- Avantages : un seul code d'écrans ; un thème est une donnée (jetons W3C Design Tokens) plus des choix pris dans une liste fermée de points de mise en page (densité, grille ou liste de couvertures, en-tête de série, largeur de navigation) ; contrôle de contraste automatisable ; ajout d'un thème sans code d'écran.
- Inconvénients : les directions les plus éloignées (Console, Galerie) sont un peu rabotées pour tenir dans les points déclarés ; une liste de variantes à tenir fermée.
### D. Thèmes libres (CSS utilisateur)
- Avantages : personnalisation totale.
- Inconvénients : contraste et lisibilité non garantis ; surface de support ; CSS injecté dans une SPA à CSP stricte.

## Décision
Option C. A « Bibliothèque » est le défaut de l'instance (réglable par l'admin) ; chaque utilisateur choisit son thème (`user.theme`) et `color_scheme` (`auto`, clair, sombre). Aucun composant, écran ou store ne lit le nom du thème (I51) : les composants lisent des propriétés CSS et des options typées. Jetons dans `frontend/src/lib/themes/<thème>/tokens.json`, compilés en propriétés CSS sous `:root[data-theme]`. Jalons : mécanisme et A en **M3** (la 1.0 ne livre qu'une direction, Q18 respectée) ; B, C, D et le sélecteur en **M4**, coupables par la règle des 150 % ; `/lite` (e-ink, M5) reste hors thèmes. Option D rejetée.

## Conséquences
- Positives : le choix demandé par le porteur sans multiplier les écrans ; un thème de plus = un fichier de jetons et une ligne de variantes ; contraste vérifié en CI.
- Négatives : tests visuels × 4 thèmes × 2 schémas sur les écrans clés ; les maquettes B et C sont adaptées aux points de variante, pas reproduites au pixel.

## Risque principal
Dérive des thèmes secondaires (R71). Mitigation : contraste AA bloquant par thème, captures Playwright par thème sur les écrans clés, analyse statique « aucun composant ne lit le nom du thème », B/C/D coupables avant A.

## Réversibilité
Haute : retirer un thème = retirer un fichier de jetons (les utilisateurs retombent sur le défaut) ; revenir à l'option A = figer le sélecteur.

## Exigences liées
EF-UX-012, ENF-A11Y-001, ENF-I18N-001, ENF-SEC-004, I51, ADR-0013.

## Sources
- https://www.w3.org/TR/WCAG22/#contrast-minimum
- https://www.designtokens.org/tr/drafts/format/
- https://github.com/design-tokens/community-group
- https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-color-scheme
- https://www.shadcn-svelte.com/docs/theming
- `deliverables/front-propositions.html`, `review/ARBITRAGES-porteur2.md`
