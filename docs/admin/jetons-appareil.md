# Jetons d'appareil (liseuses)

Un jeton d'appareil est le « mot de passe » d'une liseuse : il sert à l'OPDS de Shelfhold (HTTP Basic) et
à la synchronisation kosync de KOReader. Il n'est **jamais** le mot de passe d'un compte.

## Ce qu'un jeton permet, et rien d'autre

- Il appartient à **un profil lecteur**, nommé à l'émission, sans valeur par défaut. Les droits et les
  filtres sont ceux de ce profil, lus à chaque requête : un enfant ne voit rien au-dessus de son plafond,
  un plafond abaissé joue dès la requête suivante. Qui émet le jeton (l'hôte, ou un admin) n'y change rien.
- Il n'ouvre que les routes `/opds` et `/kosync`. Aucune route d'administration, aucune route ordinaire de
  l'API, aucune action réservée (clés, profils, classement, purge…) : présenté ailleurs, il n'est personne.
- Un profil **administrateur** ne porte pas de jeton (refusé) : une liseuse perdue ouvrirait tout le
  catalogue, sans plafond.
- Il se révoque **à l'instant**. Une réinitialisation du mot de passe du profil (`admin reset-password`),
  un changement de son rôle (même règle que pour ses sessions et ses clés) et une restauration de
  sauvegarde révoquent aussi ses jetons ; le changement de rôle est inscrit à l'audit.

## L'émettre depuis l'écran « Connecter une liseuse »

Réglages → « Jetons d'appareil (liseuses) » (`/appareils/jetons`). L'écran écrit l'adresse du catalogue
(`/opds`, avec le préfixe de `root_path` ou du mandataire de confiance) et celle de la synchronisation
(`/kosync`), l'heure du serveur, et rappelle que HTTPS est exigé. Un code QR porte l'adresse du
catalogue **seulement** : jamais l'identifiant ni le secret.

- Un **membre** crée et révoque **ses** jetons. Un **administrateur** crée un jeton pour un membre ou un
  enfant (jamais pour un administrateur, donc jamais pour lui-même) et révoque ceux de tous. Un
  **enfant** n'en gère aucun : un parent le fait pour lui. Le jeton d'un autre profil répond comme un
  jeton inconnu.
- La création est une **action réservée** (`device_token.issue`) : session récente (900 s par défaut)
  dans tous les profils, jamais une clé d'API ni un jeton d'appareil ; la page redemande le mot de passe
  sur place si besoin. La révocation est `device_token.manage`. Les deux sont inscrites à l'audit.
- Le **secret** s'affiche **une seule fois**, tel qu'il se saisit (26 caractères, sans séparateur), dans
  la réponse de la création ; il n'est ni dans l'adresse, ni dans le stockage du navigateur, ni dans un
  journal, ni dans la liste, et la page le retire quand on a noté le secret ou qu'on la quitte.
- L'écran rappelle trois choses : réglez KOReader sur la méthode « **binaire** » (pas « nom de
  fichier »), une liseuse n'a **qu'un serveur de synchronisation** (pointée vers Shelfhold, les fichiers
  pris sur le catalogue de Kavita ne se synchronisent plus), et une liseuse **en avance** sur l'horloge
  ignore en silence les positions plus récentes (KOReader n'envoie aucune heure) : comparez avec
  l'heure du serveur affichée.

## Le registre d'appareils (M5)

Chaque appairage fait **un appareil** : un libellé, un type (`koreader`, `kobo`, `opds`, `browser`,
`other`), le profil qu'il lit, et « vu pour la dernière fois » (la dernière requête acceptée). L'écran
« Connecter une liseuse » les liste (`GET /api/v1/devices`) : un membre voit ceux de son profil, un
administrateur ceux de tous, un enfant aucun. Un appareil d'un autre profil répond comme un
appareil inconnu.

- **Nouveau secret** (`POST /api/v1/devices/{id}/rotate`, action réservée `device_token.rotate`, session
  récente) : un nouveau secret sur le **même** appareil. **L'ancien est refusé dès la requête suivante**
  (pas de période de grâce) ; l'identifiant, le profil et l'échéance restent. Il suffit de ressaisir le
  secret dans la liseuse, rien n'est à réappairer. Le secret n'est montré qu'une fois. Un jeton expiré
  demande une nouvelle durée (`expires_days`).
- **Retirer** (`DELETE /api/v1/devices/{id}`, action réservée `device.remove`) : tous les jetons de
  l'appareil sont révoqués à l'instant. Une réinitialisation du mot de passe du profil, un changement de
  rôle et une restauration retirent aussi les appareils.
- Rien de tout cela n'est ouvert à une clé d'API ni à un jeton d'appareil. Ni le secret, ni son md5, ni
  l'en-tête `x-auth-key` n'entrent dans un journal.

### Les refus du registre

| Code | Ce qui s'est passé | Ce qu'il faut faire |
|---|---|---|
| `device_token_not_found` | le jeton est inconnu, déjà révoqué, ou n'est pas le vôtre | actualisez la liste de vos appareils |
| `device_rotation_conflict` | une autre rotation du même appareil a été écrite la première | actualisez la liste, puis recommencez si le secret affiché ailleurs n'est pas celui que vous voulez |
| `device_tls_required` | la requête est arrivée en HTTP simple | servez Shelfhold derrière TLS, ou, sur un réseau local de confiance seulement, mettez `SHELFHOLD_DEVICE_ALLOW_PLAIN_HTTP` à vrai |

## L'émettre, le lister, le révoquer en ligne de commande

```
shelfhold admin device-tokens create --profile NOM [--name "Liseuse du salon"] [--kind koreader] [--expires-days N]
shelfhold admin device-tokens list [--json]
shelfhold admin device-tokens revoke IDENTIFIANT
shelfhold admin device-tokens rotate IDENTIFIANT_APPAREIL [--expires-days N]
shelfhold admin device-tokens remove IDENTIFIANT_APPAREIL
```

`rotate` et `remove` prennent l'identifiant de l'**appareil** (première colonne de `list`). `rotate` affiche
le nouveau secret sur la sortie standard, une fois ; l'ancien est refusé aussitôt.

`create` affiche l'**identifiant** (8 caractères, public) sur la sortie d'erreur et le **secret** (26
caractères, sans séparateur) sur la sortie standard, **une seule fois**. Saisissez-le **tel quel** dans
KOReader : kosync reçoit le md5 du texte saisi et le serveur ne peut pas le normaliser, donc un espace
ou une majuscule de trop donne un 401. Le secret n'est conservé nulle part : la liste montre l'identifiant, le
profil, le nom, l'échéance et le dernier usage.

## Ce que cela implique

- Le secret reste **en clair** dans les réglages OPDS de KOReader, et sous forme d'`md5` (c'est ce que
  KOReader envoie) dans ceux de kosync. **La révocation est la seule parade à la perte de la liseuse.**
- **Le md5 vaut le secret pour kosync** : qui lit `x-auth-key` (réseau en clair, réglages de KOReader)
  rejoue kosync sans connaître le secret. Il n'ouvre **pas** l'OPDS : la porte Basic exige le secret
  lui-même et refuse un md5 présenté à sa place.
- **HTTPS est exigé** (ADR-0053 §4). En HTTP simple, `/opds` et `/kosync` refusent la requête avant toute
  vérification (`device_tls_required`). Seul le réglage `SHELFHOLD_DEVICE_ALLOW_PLAIN_HTTP=true` (faux par
  défaut) le permet : réservé à un réseau local de confiance, car quiconque lit ce réseau lit le secret et
  rejoue le md5. Derrière un mandataire déclaré dans `SHELFHOLD_HTTP_TRUSTED_PROXIES`, `X-Forwarded-Proto`
  fait foi ; sans mandataire de confiance, l'en-tête est ignoré.
- Après 5 secrets faux par minute pour un même couple (adresse, identifiant), même le bon secret attend la
  minute suivante (`429` et `Retry-After`). Une même adresse qui échoue 30 fois reçoit des `429` pour ses
  essais faux, mais un secret **juste** passe toujours : une liseuse n'est pas bloquée par le voisin qui
  partage son adresse. Déclarez le mandataire inverse dans `SHELFHOLD_HTTP_TRUSTED_PROXIES`, sinon toutes
  les liseuses partagent son adresse.
- La limitation vit en mémoire (un processus par instance) : elle borne l'essai, elle n'est pas une
  protection cryptographique ; c'est le secret de 130 bits qui l'est.
