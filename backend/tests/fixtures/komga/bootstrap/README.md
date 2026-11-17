# Amorçage du Komga réel (nightly, L44)

`compose.yaml` lance l'image officielle de Komga (`KOMGA_IMAGE`, choisie par `nightly.yml` :
version épinglée) sur un réseau interne. `bootstrap.json` décrit l'administrateur créé par la
revendication d'une installation fraîche (`POST /api/v1/claim`, en-têtes `X-Komga-Email` et
`X-Komga-Password`), la clé d'API obtenue par `POST /api/v2/users/me/api-keys` et les délais
d'attente. Aucune source réelle de contenu : les CBZ sont fabriqués par les tests
(`tests/nightly/komga_real.py`).
