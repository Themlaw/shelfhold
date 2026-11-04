# Décisions d'exécution du jalon M1a

Ces décisions précisent ou amendent les ADR pour l'exécution de M1a. Une section par lot.

## L03 : schéma catalogue et acquisition (migration 0003)

1. **A1, nommage de la liaison.** Le nom `source_binding` est conservé (`edition_id NOT NULL`, `priority` unique parmi les liaisons actives d'une Édition par index partiel). `series_follow` a pour clé `edition_id`. Les deux portent une clé étrangère composite `(edition_id, work_id)` vers `edition`.
2. **Écart : `edition.packaging` est dans la clé d'unicité dès M1a.** La spécification (02, GAP-8-03) l'y ajoute en M2. La colonne a pour sentinelle `''` : le comportement des clés existantes ne change pas, et M2 n'aura pas à reconstruire l'index unique de la table centrale. À acter par l'amendement d'ADR-0021 à la prochaine passe sur le modèle. Sans objection, la colonne reste dans la clé.
3. **Place des Unités non numérotées.** La clé `(edition_id, track, number_segment, number_norm, part)` vaut pour les pistes numérotées. La piste `unnumbered` est unique par `(edition_id, source_order)` : avec la clé générale, une Édition ne pourrait avoir qu'un seul extra sans numéro.
4. **`variant_revision` en ajout seul.** UPDATE refusé ; DELETE refusé tant que la Variante existe (l'effacement EF-BIB-052 supprime la Variante, donc ses révisions) ; `INSERT OR REPLACE` d'une révision existante refusé par un trigger BEFORE INSERT, car SQLite ne déclenche pas les triggers DELETE d'un REPLACE sans `recursive_triggers`.
5. **Effacement et tickets de téléchargement.** `download_request.variant_id` est `ON DELETE SET NULL`, accepté seulement pour l'état `closed_by_purge` : la purge ferme d'abord les tickets, puis supprime la Variante et l'Unité.
6. **Enveloppe scellée des secrets** (`source_config`, `source_account`) : `secret_kid`, `secret_v`, `secret_nonce`, `secret_ct`, tout ou rien. L'algorithme est lu dans `secret_key.alg` ; la longueur du nonce n'est donc pas figée. Le cookie jar est dans le document scellé.
7. **Garanties « même Édition » non posées par le schéma.** `team_priority` vérifie par clé composite que l'équipe appartient à l'Édition. Pour `unit_variant.team_id` et la liaison d'une Variante, `unit_variant` n'a pas de colonne `edition_id` : il faudrait la dénormaliser, et `ON DELETE SET NULL` sur l'équipe effacerait aussi l'Édition. Reste un invariant d'application, à couvrir par la suite de propriétés des invariants du modèle (03, ENF-QUAL-006).
8. **`edition.shelf_id` n'existe pas en 0003.** La migration 0004 (L04) le pose avec la table `shelf` : `ADD COLUMN shelf_id` nullable, clé étrangère vers `shelf.id`. Une Édition sans étagère a `shelf_id` nul.
9. **Hors L03** (étendus plus tard, pas de colonne anticipée) : `work_redirect`, `merge_log`, `metadata_field`, `external_link` (M2 à M4), `team.number_offset` (M3), `plugin_install.signer_pubkey` et `index_url` (M6). `work_relation` est créée : 02 en définit les colonnes (crochet V1, EF-BIB-039).
