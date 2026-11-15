# Jetons d'appareil (liseuses)

Un jeton d'appareil est le « mot de passe » d'une liseuse : il sert à l'OPDS de Shelfhold (HTTP Basic) et
à la synchronisation kosync de KOReader. Il n'est **jamais** le mot de passe d'un compte.

## Ce qu'un jeton permet, et rien d'autre

- Il appartient à **un profil lecteur**, nommé à l'émission, sans valeur par défaut. Les droits et les
  filtres sont ceux de ce profil, lus à chaque requête : un enfant ne voit rien au-dessus de son plafond,
  un plafond abaissé joue dès la requête suivante. Qui émet le jeton (l'hôte, ou un admin) n'y change rien.
- Il n'ouvre que les routes `/opds` et `/kosync`. Aucune route d'administration, aucune route ordinaire de
  l'API, aucune action réservée (clés, profils, classement, purge…) : présenté ailleurs, il n'est personne.
- Il se révoque **à l'instant** ; une réinitialisation du mot de passe du profil (`admin reset-password`) et
  une restauration de sauvegarde révoquent aussi ses jetons.

## L'émettre, le lister, le révoquer

```
shelfhold admin device-tokens create --profile NOM [--name "Liseuse du salon"] [--expires-days N]
shelfhold admin device-tokens list [--json]
shelfhold admin device-tokens revoke IDENTIFIANT
```

`create` affiche l'**identifiant** (8 caractères, public) sur la sortie d'erreur et le **secret** (26
caractères en groupes de 4) sur la sortie standard, **une seule fois**. Dans KOReader, saisissez le secret
sans espaces ni tirets, en minuscules. Le secret n'est conservé nulle part : la liste montre l'identifiant, le
profil, le nom, l'échéance et le dernier usage.

## Ce que cela implique

- Le secret reste **en clair** dans les réglages OPDS de KOReader, et sous forme d'`md5` (c'est ce que
  KOReader envoie) dans ceux de kosync. **La révocation est la seule parade à la perte de la liseuse.**
- Servez Shelfhold en **HTTPS** : un Basic ou un `x-auth-key` en clair sur le réseau serait rejouable.
- Après 5 secrets faux par minute pour un même couple (adresse, identifiant), ou 30 pour une même adresse,
  même le bon secret attend la minute suivante (`429` et `Retry-After`). Derrière un mandataire inverse,
  déclarez-le dans `SHELFHOLD_PROXY_TRUSTED_CIDRS`, sinon toutes les liseuses partagent l'adresse du
  mandataire.
