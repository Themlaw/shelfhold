# Exposer Shelfhold hors de chez vous

Par défaut le compose publie le port sur `127.0.0.1:8080` seulement. Rien n'est pensé pour être
exposé directement à Internet en M1a, et l'interface web n'existe pas encore : l'administration se
fait en ligne de commande, qui ne passe pas par le réseau.

Si vous avez besoin d'atteindre l'API à distance (clé d'API, outil tiers) :

1. Servez Shelfhold derrière un proxy inverse ou un tunnel qui fait le **HTTPS**, et définissez
   `SHELFHOLD_PUBLIC_URL=https://…` : les cookies prennent alors `Secure` et le préfixe `__Host-`,
   et toute adresse absolue est tirée de cette valeur, jamais de l'en-tête `Host`.
2. Activez le [profil durci](durcir.md) : sessions courtes, clés d'API à expiration obligatoire.
3. Ne publiez **pas** Kavita, Suwayomi ni la socket `/data/shelfhold.sock`. Cette socket n'est
   jamais exposée par le réseau, et `SHELFHOLD_CLI_ALLOW_HOST` doit rester faux.
4. Pensez à la limitation des tentatives : elle compte par adresse, et derrière un proxy toutes les
   requêtes semblent venir de lui jusqu'à M3 ([durcir.md](durcir.md)). Doublez-la au niveau du proxy.
5. Retirez au proxy les en-têtes d'identité (`Remote-User`, `X-Forwarded-User`…) : Shelfhold ne
   les croit pas, mais `shelfhold status` signale ceux qu'il retire.
6. La page de connexion affiche le lien « Code source » obligatoire (licence AGPL) avec le tag de
   la version, ce qui révèle cette version. Pour une instance non modifiée, vous pouvez faire
   pointer `SHELFHOLD_SOURCE_URL` vers le dépôt sans tag.

Un compte par lecteur dans Kavita, sans compte adulte ouvert sur un appareil partagé, est la
seule protection du contrôle parental : voir [durcir.md](durcir.md).
