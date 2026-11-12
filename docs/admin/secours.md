# Accès de secours, sessions et kit de secours

Perdre son mot de passe, un appareil, ou la phrase de passe de l'export du trousseau ne doit pas
vous enfermer dehors. Shelfhold n'a **aucun code de récupération sur le web** : un enfant qui en
trouverait un contournerait la barrière parentale. La reprise passe toujours par l'**hôte**, c'est-à-dire
par quelqu'un qui a la main sur la machine ou sur le conteneur.

## Retrouver l'accès : `admin reset-password`

Depuis l'hôte, dans le conteneur (le serveur peut tourner ou être arrêté) :

```sh
docker compose exec shelfhold shelfhold admin reset-password \
    --username NOM --password-file /data/nouveau.pass
```

Le mot de passe vient d'un fichier ou de l'entrée standard (`--password-file -`), jamais de la ligne
de commande. Toutes les sessions et toutes les clés d'accès (passkeys) de ce compte sont fermées ; les
autres comptes ne changent pas. L'action est journalisée (sans mot de passe). Aucune route web ne
fait cela.

## Voir et fermer les sessions

Chaque profil voit ses **propres** sessions dans son compte : l'appareil (« Firefox · Linux »,
jamais l'en-tête brut), la date de la dernière saisie du mot de passe (`auth_time`), la dernière
activité et l'adresse. Il peut en fermer une, ou fermer toutes les autres d'un coup. Une clé d'API
ou un jeton d'appareil ne peut ni lister ni fermer une session.

L'administrateur voit celles de tous les profils (`/api/v1/admin/sessions`) et peut en fermer une :
c'est une **action réservée** (session d'administrateur, jamais une clé ; session fraîche dans le
profil durci). Depuis l'hôte, `shelfhold admin sessions revoke --all` (ou `--user NOM`, ou un
identifiant) ferme sans arrêter le serveur : c'est le geste d'un appareil volé.

## Le kit de secours imprimable

L'administrateur télécharge le kit à `/api/v1/admin/recovery-kit` (une page HTML sans script, faite
pour l'impression). **Imprimez-le et rangez-le hors de l'appareil.** Il dit :

- où noter la phrase de passe de l'export du trousseau (il ne la contient pas) ;
- la commande de reprise (`admin reset-password`) et celle qui ferme toutes les sessions ;
- comment continuer à lire dans Kavita sans Shelfhold ;
- comment suspendre les suivis (mode maintenance) pendant la réparation ;
- le nom du fichier d'export du trousseau (`keyring.age`), sans son contenu.

Le kit **ne contient aucun secret** : ni mot de passe, ni phrase de passe, ni clé du trousseau, ni
jeton. Le fichier d'export du trousseau se garde dans un autre endroit que la phrase de passe qui
le déchiffre (voir [restauration.md](restauration.md) et [secrets.md](secrets.md)).

**Refaites l'export du trousseau et réimprimez le kit après chaque rotation** du trousseau : un
export plus ancien ne contient pas la nouvelle clé, et sans elle le journal des suppressions ne
protège plus rien après une perte de `/data`.

## Jetons d'appareil (liseuses)

Un jeton d'appareil porte le profil lecteur pour lequel il est émis (`subject_user_id`) et
l'administrateur qui l'a émis (`issued_by`). Les droits et les filtres, dont le contrôle parental,
sont ceux du **profil lecteur**, jamais ceux de l'administrateur. L'émission et l'appairage sont
livrés en M5 ; la règle est déjà posée et testée.
