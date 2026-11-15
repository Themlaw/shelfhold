# Guide de séance : test d'utilisabilité de la 1.0 (D3)

Source : [`docs/spec/03-roadmap-tdd.md`](../spec/03-roadmap-tdd.md) (définition de fini de la 1.0), ENF-UX-001, ENF-COMP-002, D3 de [`docs/decisions/M3.md`](../decisions/M3.md).

Le tag `v1.0.0-rc.1` est posé à la fin de M3. Ce test, tenu par le mainteneur, décide du tag `v1.0.0` : **chaque tâche doit être réussie sans aide par au moins 4 personnes sur 5**. Sinon, une itération ciblée précède le tag (pas de 1.0 sans ≥ 4/5). Aucun outil ne le remplace : le parcours chronométré de la CI (`test_first_book_under_10_minutes`) mesure le produit, pas la compréhension d'une personne.

## Avant la séance

- Une installation neuve de la `rc.1` sur l'image de release, avec Kavita connecté et une source de livres (Project Gutenberg ou Wikisource, libre de droits aux États-Unis) active. Un compte administrateur, un profil enfant (plafond 13+) et un profil membre.
- Au moins **5 personnes** du foyer ou proches, dont un enfant si possible. Une personne par séance, l'une après l'autre : personne ne voit la séance d'une autre.
- Une personne qui observe et chronomètre (le mainteneur) ; elle ne parle que pour lire la consigne.
- Une liseuse ou une tablette pour la tâche 5. Un appareil par famille de la [matrice des clients](../admin/clients.md) (navigateur de bureau, Safari iOS, Boox, Kindle, Kobo, KOReader), au moins un par famille que le foyer possède vraiment.

## Règles de conduite

1. Lire la consigne, rien d'autre. Ne pas montrer, ne pas décrire un écran, ne pas confirmer qu'on est « sur la bonne voie ».
2. Une aide donnée, même d'un mot, **fait échouer la tâche** pour cette personne. L'échec est un résultat, pas un incident : on note où la personne s'est arrêtée et on passe à la tâche suivante.
3. Une tâche est abandonnée après 5 minutes (10 pour la tâche 4). Elle compte comme un échec.
4. Pas de jugement sur la personne : on mesure l'interface.
5. Aucune donnée personnelle (voir plus bas).

## Les cinq tâches

Chacune se joue depuis l'état de départ indiqué ; l'observateur le rétablit entre deux personnes.

| # | Tâche (consigne lue à voix haute) | Profil | État de départ | Réussite |
|---|---|---|---|---|
| 1 | « Trouvez un manga et ajoutez-le à la bibliothèque. » | administrateur | installation neuve, une source active | le titre est choisi dans la recherche et l'ajout est lancé, sans aide |
| 2 | « Vous êtes l'enfant : demandez un livre. Puis, en administrateur, approuvez la demande. » | enfant, puis administrateur | profil enfant créé, une source « famille » active | la demande est créée par l'enfant, puis acceptée dans « À approuver » |
| 3 | « Retrouvez ce livre dans Kavita. » | administrateur | le livre de la tâche 1 ou 2 est arrivé | la personne ouvre Kavita et trouve le livre |
| 4 | « Une alerte dit que Kavita est injoignable : comprenez-la et corrigez le problème. » | administrateur | Kavita arrêté par l'observateur avant la tâche | la personne lit la carte d'erreur (code, cause, action), suit l'action proposée et la page Santé repasse au vert |
| 5 | « Connectez votre liseuse à la bibliothèque. » | administrateur | l'écran « Connecter un appareil » existe ; la liseuse est à portée | l'adresse OPDS et l'accès sont posés sur l'appareil et le catalogue s'affiche |

## Relevé par personne et par tâche

Un tableau par séance, rempli à la main puis agrégé. **Une ligne par personne, désignée par un numéro (P1 à P5)**, jamais par son nom.

| Personne | Tâche | Réussie sans aide (oui / non) | Durée | Où elle s'est arrêtée ou ce qu'elle a dit | Aide donnée (oui entraîne « non ») |
|---|---|---|---|---|---|

Agrégat à publier (dans `docs/recherche/`, sans autre détail) :

| Tâche | Réussites / 5 | ≥ 4/5 ? | Cause principale des échecs | Correctif décidé |
|---|---|---|---|---|

## Relevé du User-Agent par famille d'appareil

La détection d'un client se fait par capacités, jamais par User-Agent ([clients](../admin/clients.md)). Le User-Agent n’est relevé qu'**à titre d'information**, pour remplir la matrice de clients (ENF-COMP-002) : une vérification sur appareil réel par famille.

Pour chaque famille d'appareil réellement essayée, l'observateur note :

| Famille | Modèle et version du système (générique, par exemple « liseuse e-ink, Android 11 ») | User-Agent relevé (chaîne telle qu'affichée) | Version du navigateur | L'écran de recherche s'affiche | L'accès OPDS de Kavita fonctionne avec un compte restreint | Téléchargement hors ligne | Remarques |
|---|---|---|---|---|---|---|---|

Les familles : navigateur de bureau, Safari iOS, Boox, Kindle, Kobo, KOReader, Kover. Une famille non essayée reste « non vérifié » dans la matrice : on n'écrit jamais une case que personne n'a vue. Le relevé se fait dans le gabarit d'issue « rapport d'appareil » (`.github/ISSUE_TEMPLATE/rapport-appareil.yml`). Le cas « OPDS de Kavita avec un compte restreint » fait partie de la vérification.

## Aucune donnée personnelle

- Ni nom, ni prénom, ni adresse, ni photo, ni enregistrement audio ou vidéo des personnes. Un numéro (P1 à P5) et, pour un enfant, « profil enfant ».
- Pas d'âge exact : « enfant », « adulte ».
- Aucune capture d'écran qui montre un compte, une adresse de la maison, un jeton ou un titre privé. Les captures éventuelles se font sur l'installation de test.
- Les notes brutes restent privées et sont détruites après l'agrégat. Seul l'agrégat (tableau ci-dessus, par tâche) se publie.
- Avant la séance, expliquer à la personne ce qui est noté et qu'elle peut arrêter à tout moment ; pour un enfant, l'accord de son parent.
- Les User-Agent relevés sont ceux d'**appareils**, pas de personnes : on retire tout identifiant d'appareil (numéro de série, nom donné à l'appareil) avant de les publier.

## Après la séance

1. Agréger, décider : ≥ 4/5 sur les cinq tâches → `v1.0.0` ; sinon, lister les correctifs, les faire, puis refaire **seulement les tâches échouées** avec de nouvelles personnes.
2. Mettre à jour la [matrice des clients](../admin/clients.md) avec les familles vérifiées.
3. Consigner le résultat dans `docs/decisions/M3.md` (section « Clôture ») et le journal des portes ([portes](portes.md)) : c'est le test de substitution à la porte P2 (le test concierge n'a pas eu lieu).

## Questions au juriste (D4, Q27b)

Le tag `v1.0.0` n'attend pas l'avis ; **l'annonce publique l'attend** (porte P3). Le dépôt reste public et non promu jusqu'à l'avis. Les questions, à remettre avec la politique écrite ([`docs/legal/`](../legal/cra.md), ADR-0015, ENF-LEG-001 à 007) :

**1. Cadre familial (bibliothèque personnelle du foyer).**
- Une copie faite par l'utilisateur depuis une source licite, puis lue par les membres de son foyer via Kavita, relève-t-elle de la copie privée (CPI L122-5 2° ; CJUE, C-435/12 : source licite seulement) ou de la représentation dans le cercle de famille (L122-5 1°) ? À quelles conditions (même foyer, pas d'accès hors du cercle) ?
- Le projet doit-il limiter techniquement le nombre de comptes ou l'accès hors du réseau local, ou une documentation suffit-elle ?
- Le vocabulaire retenu (« bibliothèque personnelle du foyer », sans le vocabulaire de la diffusion) est-il adéquat ?

**2. Domaine public par pays.**
- Un avertissement indicatif, non bloquant, suffit-il quand une source libre de droits aux États-Unis propose une œuvre encore protégée en France (prorogations de guerre, « mort pour la France », droits du traducteur) ?
- Le vocabulaire « libre de droits aux États-Unis » est-il adéquat ?

**3. Fichiers tatoués.**
- La réécriture ciblée de l'OPF d'un fichier, toutes les autres entrées étant copiées octet pour octet, respecte-t-elle CPI L331-11 ?
- L'exclusion des fichiers tatoués des paquets de diagnostic et des destinations hors du foyer protège-t-elle suffisamment les données personnelles de l'acheteur ?

**4. Cyber Resilience Act (règlement UE 2024/2847).**
- Le projet, tenu par un mainteneur individuel sans monétisation, est-il bien hors champ ?
- Quels faits le feraient basculer (dons, hôte fiscal, association, support payant, redistribution par un vendeur de NAS) ?
- Une association porteuse serait-elle un « steward » au sens de l'article 24, et avec quelles obligations (politique de cybersécurité, signalement des vulnérabilités activement exploitées) ? Support : [`docs/legal/cra.md`](../legal/cra.md).

Sujets du même avis, déjà listés par Q27b : la politique sur les DRM et la posture d'ensemble, les conditions d'utilisation des fournisseurs de métadonnées, la relecture finale de l'exception de plugin (ADR-0036).
