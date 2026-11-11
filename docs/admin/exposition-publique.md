# Exposer Shelfhold hors de chez vous

Par défaut le compose publie le port sur `127.0.0.1:8080` seulement. Rien n'est pensé pour être
exposé directement à Internet, et l'interface web se limite pour l'instant aux écrans « À classer » et
« À ranger » : l'administration se fait en ligne de commande, qui ne passe pas par le réseau.

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

## Discrétion de l'instance (ENF-PRIV-002)

Une instance exposée ne doit ni se faire indexer, ni s'afficher en aperçu dans une messagerie.
Shelfhold y veille de son côté ; ce qui suit relève de vous.

**Ce que Shelfhold fait.** Toutes les réponses portent `X-Robots-Tag: noindex, nofollow` ;
`/robots.txt` répond `Disallow: /` ; aucune balise Open Graph ni Twitter Card n'est émise ; la
page de connexion est neutre (titre générique, aucune bibliothèque ni couverture) ; toute route de
contenu exige une session ou une clé, de sorte qu'un lien collé dans une conversation ouvre la page
de connexion et jamais un aperçu.

**Ce que vous faites.**

1. **Déclarez votre domaine dans la Search Console** du moteur de recherche que vous utilisez : vous
   y verrez ce qui est indexé, et vous pourrez demander un retrait en cas d'erreur. Le fichier de
   vérification ou l'enregistrement DNS qu'elle demande ne doit pas passer par Shelfhold (servez-le
   depuis le proxy).
2. **N'exposez aucune démo ni page de présentation sur le domaine de l'instance** : un domaine qui
   mêle une démonstration publique et une bibliothèque privée est jugé sur l'ensemble de son
   contenu. Mettez la démo sur un autre nom de domaine.
3. **Une signalisation « Navigation dangereuse » (Safe Browsing) ne se contourne pas, elle se
   traite.** Procédure de recours :
   1. vérifiez l'état du domaine dans le rapport « Problèmes de sécurité » de la Search Console ;
   2. retirez ce qui a pu être signalé : démo, page d'accueil publique, compte invité, tout
      contenu servi sans connexion ;
   3. confirmez que `curl -I` sur la racine, `/robots.txt` et une route de contenu renvoie bien les
      en-têtes ci-dessus et une redirection ou un refus, jamais un aperçu ;
   4. déposez la demande de réexamen depuis la Search Console, en décrivant ce qui a été corrigé ;
   5. en attendant, travaillez par le réseau local ou le tunnel de votre proxy : la CLI
      ([headless.md](headless.md)) ne passe pas par le réseau.
4. Gardez le proxy sans page par défaut : une réponse aimable sur `/` à un visiteur anonyme est un
   aperçu en puissance.
