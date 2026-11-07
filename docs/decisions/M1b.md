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

## L05 : passkeys (WebAuthn), revue de la PR #95

1. **Enregistrer et révoquer une passkey sont des actions réservées** (`passkey.register`, `passkey.revoke`, effet `manages_credentials`). Comme créer une clé d'API : toute clé d'API est refusée (403 `credential-not-allowed`) ; la fraîcheur de 900 s (`reauth-required`) ne vaut qu'en **profil durci**. L'audit porte `session_id` et `auth_time`. Réservée à l'admin comme le reste du registre : avant M3 il n'existe pas d'autre compte ; M3 élargira les rôles de ces deux lignes.
   **Question pour le porteur :** hors profil durci, une session volée (30 jours glissants) peut ajouter une passkey qui survit au changement de mot de passe de l'attaquant... sauf `admin reset-password`, qui les révoque toutes. Le défaut actuel est « aucune fraîcheur » (arbitrage P3, liste fermée) ; exiger 900 s par défaut pour l'enregistrement seul serait une décision de produit.
2. **`admin reset-password`** change le mot de passe, ferme les sessions et révoque les passkeys dans **une seule transaction `BEGIN IMMEDIATE`**. L'écriture d'une session ouverte par passkey revérifie, dans sa propre transaction, que la passkey est encore vivante : une connexion qui a passé la vérification juste avant la réinitialisation n'ouvre aucune session.
3. **Sessions et passkey révoquée (migration 0015).** `session.passkey_id` garde la passkey qui a ouvert la session ; révoquer la passkey ferme ces sessions, dans la même transaction. Pas de clé étrangère : SQLite ne l'ajoute qu'en reconstruisant la table, et une passkey n'est jamais supprimée (révoquée), sauf avec son compte. La ré-authentification par passkey ne change pas cette colonne.
4. **RP ID mémorisé (migration 0015).** `webauthn_credential.rp_id` enregistre le domaine de chaque passkey ; les lignes existantes reçoivent celui de `SHELFHOLD_PUBLIC_URL` au moment de la migration (NULL sans URL). `shelfhold status` avertit (`passkey_rp_changed`) quand un RP ID de passkey vivante n'est plus celui de l'instance, port compris dans l'origine mais pas dans le RP ID. Dictionnaire de données régénéré par `release-assets.sh`.
5. **Hôte de `SHELFHOLD_PUBLIC_URL`.** Le RP ID et l'origine attendue sont en ASCII (punycode pour un IDN) et sans point final, comme ceux du navigateur ; un hôte invalide (étiquette vide ou trop longue, caractère interdit) rend les passkeys indisponibles avec le message « adresse publique invalide ».
