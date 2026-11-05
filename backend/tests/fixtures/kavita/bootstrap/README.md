# Amorçage du Kavita réel (nightly, L40)

`compose.yaml` lance l'image officielle de Kavita (`KAVITA_IMAGE`, choisie par `nightly.yml` :
version minimale 0.9.1.0, version épinglée 0.9.1.4, dernière) sur un réseau interne. `bootstrap.json`
décrit le compte créé à la première utilisation par l'API (`/api/Account/register`), la clé nommée
à échéance obtenue par l'adaptateur de production (`HttpKavitaKeyExchange`) et les délais d'attente.
Aucune source réelle de contenu : les CBZ sont fabriqués par les tests (`tests/nightly/kavita_real.py`).
