# Extension de test synthétique pour Suwayomi (nightly, ENF-LEG-013, passe 12)

Pour jouer la vraie file de téléchargement de Suwayomi (état « téléchargé », route
`GET /api/v1/chapter/{id}/download`) sans aucun contenu tiers, le dépôt construit sa propre extension.

- `server/serve.py` : serveur HTTP de fixtures (bibliothèque standard seule). Il publie deux séries
  inventées et **génère** les pages (PNG de quelques pixels) à la demande. Aucun fichier d'image.
- `extension/` : projet Gradle de l'extension (une source `Synthetic`, langue `all`) qui lit ce
  serveur à l'adresse `http://fixtures:8000`.
- `compose.yaml` : le banc de `e2e/suwayomi/compose.yaml` plus le service `fixtures`, sur le même
  réseau **interne** (aucune route vers l'extérieur).
- `build.sh` : construit l'APK (`build/synthetic.apk`), à la première étape du job, avant la coupure
  réseau du banc. Le tirage des dépendances Gradle est le seul usage du réseau de cette étape.

L'APK est installé **depuis ce fichier** (mutation GraphQL `installExternalExtension`), jamais depuis
un dépôt d'extensions : la liste des dépôts est vide et vérifiée avant l'installation. Le banc lui-même
est piloté par `backend/tests/nightly/test_suwayomi_synthetic_extension.py`.
Les noms de séries sont inventés ; ne rien y mettre qui vienne d'une source réelle.

## Chaîne de construction vérifiée

Gradle vient du wrapper du dépôt (`extension/gradlew`, version 8.10.2, `distributionSha256Sum` posé,
jar du wrapper pris au tag `v8.10.2` de gradle/gradle, sha256 `2db75c40…8046`). Le JDK est un
Temurin 17 installé par `actions/setup-java` épinglé par SHA. `extension/gradle/verification-metadata.xml`
impose les sha256 des dépendances. **Sa liste de composants est à générer** sur une machine avec le
SDK Android, puis à relire en revue : `cd extension && ./gradlew --write-verification-metadata sha256 assembleRelease`.
Tant qu'elle est vide, la construction échoue en nommant les artefacts manquants.
