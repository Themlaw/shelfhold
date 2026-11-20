# Compatibilité du contrat de plugins (`api` N-1)

Matrice publiée avec `shelfhold-sdk` 2.0.0 (M6, D10, D11, ADR-0024 amendement « SDK 2.0 », Q20 tranchée).
Le manifeste d'un plugin déclare `api` (contrat de données) ; l'hôte accepte **`api` N nativement** et
**`api` N-1 par un shim**, pendant **12 mois** après la publication de la version N.

| `api` du manifeste | `shelfhold-sdk` attendu | Chargement par l'hôte | Jusqu'à |
|---|---|---|---|
| **2** | `shelfhold-sdk>=2.0,<3` (distribution MIT, import `shelfhold_sdk`) | natif (`loaded`) | — |
| **1** | `shelfhold-sdk>=2.0,<3` (le shim réécrit le manifeste, voir ci-dessous) | par le shim (`loaded_shim`) | **2027-11** (12 mois après la publication de 2.0.0) |
| 0, 3 ou plus | — | refusé (`incompatible`), les autres plugins chargent encore | — |

## Ce que le shim `api` 1 → 2 réécrit

La rupture 2.0 est le retrait des membres propres à Kavita dépréciés depuis la série 1.x
(`parse_name_call`, `ensure_library_call`, `resolve_remote_calls`, `event_subscription`,
`progress_calls`) : un manifeste n'en porte aucun, le shim renumérote donc seulement `api`
(les champs inconnus d'un manifeste 1.x sont ignorés par le contrat). Le code du plugin, lui,
doit cesser d'appeler ces membres : ils n'existent plus dans `shelfhold_sdk.export_target`.

## Ce qui ne change pas entre `api` 1 et `api` 2

- La sérialisabilité JSON de chaque type du contrat (EF-SRC-001) : seuls des types JSON traversent
  la frontière ; aucun objet vivant (`Fetcher`, plan `FsOp` exécuté par l'hôte).
- Les ajouts de la série 1.x restent des champs facultatifs à défaut sûr : un plugin écrit pour
  1.1 charge sans changement sur un hôte `api` 2, par le shim.
- `shelfhold.sdk` (l'ancien nom d'import, réexport de `shelfhold_sdk`) existe pendant toute la
  série 1.x de l'application et disparaît avec `api` 1 : écrivez `shelfhold_sdk` dès maintenant.

## Politique de versions

- `shelfhold-sdk` suit SemVer indépendant de l'application (2.0.0 est la première version publiée).
- Une `api` dépréciée est supportée 12 mois après la publication de la version suivante
  (ENF-OPS-008) ; la date de fin est inscrite ici et dans le shim de l'hôte
  (`plugin_host.registry.SHIM_UNTIL`).
- Au-delà de la fenêtre, le manifeste est refusé avec la raison affichée, et les autres plugins
  continuent de charger.
