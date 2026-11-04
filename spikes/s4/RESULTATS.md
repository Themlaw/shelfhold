# Spike S4 : RAM au repos et taille d'image

Mesuré le 2026-11-04 par `measure.py` sur l'image `docker/Dockerfile` (build local, amd64, squelette
backend + build statique du front PR4), pile `e2e/compose.yaml` durcie (`init`, `read_only`,
`MALLOC_ARENA_MAX=2` issu de l'image). Bruts : `results.json`.

| Mesure | Valeur | Cible |
| --- | --- | --- |
| Image compressée (gzip de `docker save`, majorant du registre) | **57,5 Mo** | <= 300 Mo (ENF-PERF-002) |
| Image sur disque (`docker images`) | 244 Mo | n/a |
| RSS du processus `shelfhold serve`, 30 s après `ready` | **68,3 Mo** (Pss 64,3) | <= 250 Mo (ENF-PERF-001) |
| RSS juste après 8 000 requêtes (16 fils, `/healthz`, `/readyz`, `/openapi.json`, `/docs`, 404) | **72,0 Mo** (pic cgroup 79,0 Mo) | <= 500 Mo en pic |
| RSS 15 s après la charge | 71,7 Mo (cgroup 67,2 Mo) | stable |

## Lecture
- Au repos : 68 Mo, du même ordre que la référence 09-closure-B (83 Mo, avec davantage de dépendances).
  Marge de 180 Mo sous ENF-PERF-001 pour Huey (+3,5 Mo, voir S1), Pillow/vips et les adaptateurs.
- La charge simple fait monter le RSS de 4 Mo seulement et il ne redescend pas (arènes glibc plafonnées
  à 2 par `MALLOC_ARENA_MAX`) : pas de fuite visible sur 8 000 requêtes.
- Image : 57,5 Mo compressés, soit 19 % du plafond. La porte `docker/check-image-size.sh` (job `image`)
  échoue au-delà de 300 000 000 octets.

## Limites honnêtes
- Le « traitement d'images » de la référence (155 Mo) n'est pas mesuré : le squelette n'a ni Pillow ni libvips.
  À refaire en M1b avec la vraie charge ; ENF-PERF-001/002 n'ont pas besoin d'être ajustées à ce stade.
- Mesure amd64 uniquement en local ; la CI construit aussi l'arm64 et applique la même porte de taille.
- `content_size_mb_not_uncompressed` de `results.json` est la taille de contenu du magasin Docker, pas la
  taille décompressée (voir la ligne « sur disque »).

## Décision
Le socle (`python:3.13-slim`, uv, `MALLOC_ARENA_MAX=2`) est retenu tel quel. Aucun changement de cible.
