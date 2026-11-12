# Clés d'accès (passkeys)

Une clé d'accès remplace le mot de passe pour se connecter et pour confirmer une action sensible,
en un geste (empreinte, visage, code de l'appareil). Elle est liée au nom de domaine de votre
instance : une page qui imiterait Shelfhold ne peut pas s'en servir. Shelfhold ne stocke que la
partie publique de la clé.

## Ce qu'il faut

- **HTTPS**, ou `http://localhost`. Les navigateurs refusent les clés d'accès ailleurs. Sur
  `http://192.168.x.y` (adresse du réseau local sans TLS), la fonction n'existe pas : la page de
  connexion n'a pas de bouton, la page des clés d'accès dit pourquoi.
- **`SHELFHOLD_PUBLIC_URL`** définie avec un **nom de domaine** (`https://livres.example.org`). Le
  RP ID (le domaine auquel la clé est liée) et l'origine attendue en sont tirés, jamais de l'en-tête
  `Host` d'une requête. Une adresse IP ne convient pas : une clé d'accès se lie à un nom.
- Un authentificateur qui vérifie l'utilisateur (`userVerification=required`) et garde la clé
  (`residentKey=required`) : téléphone, ordinateur, clé de sécurité à code, gestionnaire de mots de
  passe.

## Enregistrer, utiliser, révoquer

- **Enregistrer** (et **révoquer**) : page `/compte/passkeys` (connecté), nommer l'appareil, suivre le geste. Autant de
  clés que d'appareils (25 au plus par compte).
- **Se connecter** : sur la page de connexion, « Se connecter avec une clé d'accès ». Aucun
  identifiant à saisir : la clé désigne le compte. La session est neuve et `auth_time` vaut
  l'instant de la connexion.
- **Confirmer une action sensible** (`reauth-required`) : la ré-authentification accepte une clé
  d'accès à la place du mot de passe et remet `auth_time` à jour.
- **Révoquer** : bouton sur la même page. On ne peut pas retirer son **dernier moyen de connexion** :
  sans mot de passe, la dernière clé reste.

**Enregistrer** une clé demande une authentification récente (900 s par défaut), dans tous les profils : une session volée ne peut pas ajouter une passkey qui survivrait à la révocation des sessions. En profil durci ([durcir.md](durcir.md)), **révoquer** une clé la demande aussi. Révoquer une clé ferme aussi les sessions qu'elle a ouvertes.

## Perdre un appareil, oublier son mot de passe

Aucun code de récupération n'existe sur le web. Sur la machine hôte :

    shelfhold admin reset-password --username NOM --password-file FICHIER

change le mot de passe, ferme toutes les sessions du compte et **révoque toutes ses clés d'accès**.
Reconnectez-vous avec le nouveau mot de passe, puis enregistrez de nouveau vos clés.

## Ce que Shelfhold vérifie

Chaque connexion ou ré-authentification : défi aléatoire propre à ce geste, lié à la session (ou au
flux de connexion), à usage unique et valable deux minutes ; origine et RP ID exacts (un
sous-domaine ou un autre port est refusé) ; vérification de l'utilisateur par l'appareil ; signature ;
compteur de signature qui doit avancer (un recul signale une clé clonée : refus et entrée d'audit).
Une clé qui ne compte jamais (compteur toujours à 0, cas des clés synchronisées) est tolérée.
Le défi d'une connexion est lié au navigateur qui l'a demandé par un cookie de 2 minutes ; la demande d'options est limitée à 30 par minute et par adresse. Les tentatives comptent dans la limite de connexion, comme celles du mot de passe. Chaque
enregistrement, connexion, révocation et recul de compteur est dans l'audit ; les journaux ne
contiennent ni identifiant de clé, ni clé publique, ni défi.

## Chaque enregistrement vous est signalé

Un compte peut ajouter une clé qui survit à la révocation de ses sessions : Shelfhold vous le dit à
**chaque** enregistrement, sur n'importe quel compte, y compris le vôtre.

- un événement `auth.passkey_registered` part sur vos canaux de notification (identifiants seulement : le nom
  donné à la clé n'y figure jamais) ;
- `shelfhold status` garde une ligne `passkey_registered` (nombre, comptes et noms des clés) **jusqu'à ce que vous
  l'acquittiez**, depuis l'hôte :

      shelfhold admin acknowledge-passkeys

  L'acquittement couvre tout ce que `status` montrait, il est journalisé (`passkey.registrations_acknowledged`) et
  ne fait rien s'il n'y a rien à acquitter. Une clé inconnue se révoque dans la page des clés d'accès du compte, ou
  par `shelfhold admin reset-password` (qui révoque aussi ses clés). Révoquer une clé n'acquitte pas
  son enregistrement ; un enregistrement refusé (doublon, justificatif invalide) ne signale rien.

## Changer de domaine

Les clés sont liées à l'ancien nom : après un changement de `SHELFHOLD_PUBLIC_URL` vers un autre
domaine, elles ne fonctionnent plus. Le mot de passe reste en repli ; révoquez les anciennes clés et
enregistrez-en de nouvelles. Un simple changement de port ne change pas le domaine, mais l'origine
attendue comprend le port : redémarrez Shelfhold après avoir modifié l'adresse publique.
