# Durcir une instance (profil durci)

Shelfhold applique toujours, sans réglage pour les désactiver, les **protections invisibles**
(arbitrage P1) : mots de passe hachés en argon2id, jeton CSRF sur toute requête de session qui
modifie l'état, cookies `HttpOnly` et `SameSite=Strict` (`Secure` et préfixe `__Host-` en HTTPS),
CSP par hash sur la page de connexion, limitation des tentatives de connexion, refus des
en-têtes d'identité de proxy (`Remote-User`, `Tailscale-User-*`…) tant que le forward-auth n'est
pas configuré (il ne l'est jamais avant M3), clés d'API et jetons stockés hachés.

Le **profil durci** ajoute de la friction. Il n'est jamais exigé au démarrage.

## Activer le profil durci

`SHELFHOLD_AUTH_HARDENED=true` (ou `auth_hardened = true` dans `config.toml`). Effets :

| Mesure | Défaut | Profil durci |
| --- | --- | --- |
| Session admin | 30 jours glissants, « se souvenir de moi » coché | 30 min d'inactivité, 12 h au plus, y compris pour les sessions déjà ouvertes |
| Mot de passe redemandé (`reauth-required`) | actions irréversibles ou qui révèlent un secret (purge immédiate, pierres tombales, export ou rotation des secrets, réinitialisation d'une racine, vider la corbeille, `detach` complet, confirmation post-restauration), contrôle parental dès qu'un profil restreint existe | toute action réservée (classer, démasquer, gérer les clés compris) |
| Expiration des clés d'API | facultative (aucune par défaut) | 90 jours au plus, imposés à la création |

La fenêtre de fraîcheur (`SHELFHOLD_FRESH_AUTH_S`, 900 s par défaut) vaut dans les deux profils.

## HTTPS

Servez Shelfhold en HTTPS (proxy inverse ou tunnel) et définissez `SHELFHOLD_PUBLIC_URL` en
`https://…` : les cookies prennent alors `Secure` et le préfixe `__Host-`. Sur `http://` (adresse
du réseau local sans TLS), un cookie `Secure` ne reviendrait jamais : le cookie est posé sans cet
attribut, ce qui suffit sur un réseau domestique de confiance mais pas sur un réseau partagé.

## Ce que la limitation des tentatives ne couvre pas

- 10 échecs par minute et par adresse (par /64 en IPv6), puis 429 jusqu'à la fin de la minute ;
  30 échecs par minute sur un même compte, toutes adresses confondues. Une seule adresse ne peut
  donc jamais bloquer le compte d'un autre ; un attaquant qui dispose de plusieurs adresses peut
  retarder la connexion web de ce compte le temps de son attaque. Les sessions ouvertes, les clés
  d'API et la ligne de commande de l'hôte ne sont pas touchées.
- Derrière un proxy, toutes les requêtes semblent venir du proxy jusqu'en M3 (en-tête
  `X-Forwarded-For` du seul proxy de confiance) : la limite par adresse vaut alors pour tous.
- Le compteur est en mémoire : un redémarrage le remet à zéro.

## En-têtes d'identité

Un tunnel ou un proxy qui ajoute un en-tête d'identité (par exemple `tailscale serve`, qui ajoute
`Tailscale-User-Login`) fait refuser chaque requête (401 `identity-header-refused`) : Shelfhold ne
fait confiance à aucun en-tête d'identité sans forward-auth configuré. Retirez ces en-têtes au
niveau du proxy. Le forward-auth (CIDR de confiance obligatoire, secret partagé recommandé)
arrive en M3.

## Ligne de commande de l'hôte

`shelfhold admin sessions revoke --all` ferme toutes les sessions, `shelfhold admin keys revoke
<préfixe>` révoque une clé : refus dès la requête suivante, serveur en marche ou non. L'uid qui
lance Shelfhold sur l'hôte vaut un administrateur.
