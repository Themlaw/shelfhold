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

## L'émettre, le lister, le révoquer

```
shelfhold admin device-tokens create --profile NOM [--name "Liseuse du salon"] [--expires-days N]
shelfhold admin device-tokens list [--json]
shelfhold admin device-tokens revoke IDENTIFIANT
```

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
