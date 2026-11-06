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
impose les sha256 des dépendances (254 composants). Il a été **généré** dans un conteneur avec le SDK Android
(`ghcr.io/cirruslabs/android-sdk@sha256:1c2e7e9c4490dbc1f85364556aa08e550945e6dffb29baab86d5fe2c7be0773a`, JDK 21)
par `cd extension && ./gradlew --write-verification-metadata sha256 assembleRelease`, puis la construction a été
rejouée avec la vérification active. Pour le régénérer après un changement de dépendance, relancer la même commande
dans ce conteneur et relire le diff en revue ; la liste ne se modifie jamais à la main.

La bibliothèque de l'API (`com.github.tachiyomiorg:extensions-lib`) est en `1.4.1` : la version `1.4.2` n'a jamais
été construite par JitPack. `okhttp` est une dépendance `compileOnly` (fournie par l'hôte).
