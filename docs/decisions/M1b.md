# Décisions d'exécution du jalon M1b

Ces décisions précisent ou amendent les ADR pour l'exécution de M1b. Une section par lot.

## L01 : schéma M1b (migration 0013)

La migration est la **0013** : la 0012 existe depuis la remédiation M1a L46. Les migrations 0001 à 0012 sont publiées (v0.2.0) et ne sont pas modifiées.

1. **Inventaire de ce que 0004 à 0012 ont déjà posé** (on ne crée que le manquant) :
   - `webauthn_credential` : absente. Créée.
   - `backup_run` : absente. Créée.
   - `source_config.instance_fingerprint` et `instance_fingerprint_seen_at` : absentes (seule `kavita_instance.fingerprint` existe, côté destination). Ajoutées.
   - Compteur d'erreurs de contenu consécutives : **déjà porté** par `source_failure.consecutive_content_errors`, par liaison, par `limit_key` et par classe d'échec (0003). Rien à créer.
   - `shelf.upstream_purge` : **déjà couvert** par `shelf.purge_upstream_after_export` (booléen, défaut faux, 0004), qui est le réglage de D16.
2. **Écart à D16 et au plan.** Le plan nomme la colonne `shelf.upstream_purge`. Une seconde colonne dirait la même chose que `purge_upstream_after_export` et pourrait la contredire ; renommer coûterait une reconstruction de table pour rien. Le réglage D16 est donc `shelf.purge_upstream_after_export`, désactivé par défaut, soumis à la mise à l'abri et à la double condition d'EF-BIB-038. Écart validé par l'orchestrateur.
3. **Garde de publication.** `tools/published-migrations.toml` n'est pas modifié : 0013 y sera ajoutée à la release suivante.

## L04 : rôles, droit « classer », table de vérité, `activity_view` (migration 0014)

1. **Rôles et droit « classer ».** Le modèle (`user.role`, `user.can_rate`) existait depuis 0002. Avant M3 seul l'admin porte le droit : `RATE_ROLES = {admin}` est la seule ligne que M3 élargira, et le drapeau posé à la main sur un membre n'y change rien. Un rôle inconnu, une action absente du registre ou un sosie d'action sont refusés.
2. **Visibilité (EF-USR-016, « household : visible des parents »).** Il n'existe **pas de lien « parent »** en M1b : **un adulte compte comme parent**.
   - Un `household` est visible, titre compris, de tous les adultes (admin et membres), que son auteur soit un adulte ou un enfant ; un `household` sans propriétaire aussi.
   - Un enfant ne voit que **ses propres** entrées.
   - Un `private` n'est vu que de son propriétaire ; l'admin voit celui d'un autre adulte avec le **titre masqué** et celui d'un enfant en clair.
   - Le rôle « intégration » (webhooks, outbox) ne reçoit jamais de titre ni d'entrée privée.
   - Une visibilité ou un rôle inconnus ne voient rien.
3. **D19 : demandes.** `download_request` gagne `visibility` et `requester_user_id` (nullables, migration **0014**, sans réécriture de données). Une demande sans visibilité est une opération (`ops`) : l'admin seul la voit. Le titre d'une demande est celui de l'Œuvre qu'elle vise, résolu à l'affichage. Aucune écriture du demandeur avant M3.
4. **Démasquer.** `POST /api/v1/activity/{ref}/unmask` est derrière l'action réservée `request.unmask` : le droit est vérifié avant toute lecture (403 uniforme pour une clé ou un membre, quelle que soit la ligne). Pour qui détient le droit, une entrée absente, invisible pour lui ou sans titre masqué donne le même 404. La lecture du titre et l'audit (`session_id`, `auth_time`, cible `event:<id>` ou `request:<id>`) se font dans une seule transaction.
