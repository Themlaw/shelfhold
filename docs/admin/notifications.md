# Notifications d'exploitation (Apprise)

Shelfhold prévient l'administrateur quand quelque chose demande son attention : sauvegarde en échec, disque plein, destination injoignable, tâche abandonnée, secret illisible, source cassée, nouvelle clé d'accès enregistrée sur un compte (`auth.passkey_registered`, à acquitter avec `shelfhold admin acknowledge-passkeys`), etc. (la liste est celle des événements d'exploitation, EF-INT-006). Les notifications passent par [Apprise](https://github.com/caronc/apprise), version 2.1 ou plus récente, moins de 3.

## Ce qui est accepté

Seuls ces schémas d'URL sont acceptés : `ntfy`, `ntfys`, `gotify`, `gotifys`, `matrix`, `matrixs`, `discord`, `slack`, `tgram`, `pover`. Les webhooks génériques, le courrier, les services de bureau et les schémas `http(s)` sont refusés. Préférez les variantes chiffrées (`ntfys`, `gotifys`, `matrixs`).

On les saisit avec `shelfhold config set notify.urls --value-file <fichier>` (ou la section `[notify.urls]` de `config apply`, [headless.md](headless.md)) : une ligne `nom=url` par canal, lue dans un fichier ou sur l'entrée standard, jamais en argument.

L'URL contient un jeton. Elle est chiffrée dans la base (comme les autres secrets, tournée avec la clé), n'est jamais écrite dans les journaux et ne s'affiche plus après sa saisie.

## Ce qui n'est pas protégé : la garde SSRF

**Les notifications ne passent pas par la garde SSRF du Fetcher.** Apprise fait ses propres requêtes réseau. Shelfhold en réduit le risque sans l'éliminer :

- pour un serveur auto-hébergé (`ntfy`, `gotify`, `matrix`), Shelfhold n'accepte qu'une **adresse IP** publique ou un **nom déclaré** tel quel (`hôte:port`) dans `lan_targets`. **Dans les versions 0.2 et 0.3 aucun réglage ne permet de déclarer `lan_targets`** : seule une adresse IP publique passe, un nom (public ou du réseau local) et une adresse du réseau local sont refusés. Pour une alerte vers un serveur de chez vous, utilisez un service à adresse fixe (Discord, Slack, Telegram, Pushover). Un nom public est refusé : Apprise le résoudrait lui-même à l'envoi, et un DNS malveillant pourrait alors l'envoyer vers votre réseau interne. L'adresse est refusée si elle est privée, de bouclage, de lien local ou celle du service de métadonnées du cloud (jamais permise, même déclarée) ; le contrôle a lieu à la saisie **et avant chaque envoi**, avec les `lan_targets` du moment : retirer une cible bloque les envois suivants ;
- un nom déclaré dans `lan_targets` est résolu par Apprise sans épinglage : vous lui faites confiance ;
- le certificat des variantes chiffrées est toujours vérifié (`verify=no` est refusé) ;
- les services à adresse fixe (Discord, Slack, Telegram, Pushover) ne sont pas contrôlés ;
- les redirections ne sont jamais suivies : une réponse 3xx est un échec, et aucun en-tête d'authentification ne peut être envoyé à un autre hôte.

Seul l'administrateur saisit ces URL.

## Garanties d'envoi

L'alerte est écrite dans la même transaction que l'événement, puis envoyée par une tâche : un arrêt brutal entre les deux ne la perd pas, au prix d'un doublon possible. Un envoi en échec est retenté pendant quelques heures, puis abandonné ; la bannière « Notifications en échec » (`notify.failed`) le signale, et cet événement n'est jamais renvoyé par Apprise. Plusieurs tâches abandonnées sont regroupées en un seul message.

Un message ne contient ni titre d'œuvre ni texte venu d'une source, sauf le texte amont demandé par identifiant, assaini et rendu inoffensif : les liens et les mentions (`@everyone`) y sont affichés comme du code, jamais actifs.

## Notifications par personne (EF-INT-021)

Chaque profil est prévenu à sa façon, en plus des alertes d'exploitation ci-dessus.

- **Par défaut, un résumé quotidien à 8 h**, dans le fuseau du profil, regroupé par série : trente chapitres d'une série font une ligne, pas trente messages.
- **Immédiat seulement dans deux cas** : « ta demande est arrivée » (au demandeur seul) et une panne qui dure depuis plus d'une heure (à l'administrateur seul ; une série d'échecs séparés de plus de deux heures compte pour deux pannes, chacune annoncée une fois). Un profil peut choisir le mode « immédiat » pour tout le reste.
- **Heures calmes** : de 21 h à 7 h tant que le profil n'a pas choisi les siennes. Elles retardent le résumé et l'arrivée d'une demande jusqu'à leur fin ; une panne de plus d'une heure passe malgré elles.
- **Rappel de retrait en attente** : tant qu'un `withdrawal_pending` est ouvert, chaque administrateur le retrouve une fois par jour dans son résumé. Le rappel ne fait que lire : la sortie reste `shelf withdraw`, une action réservée.
- **Ce qu'un profil peut lire** : chaque notification est projetée comme l'activité (rôle, visibilité, propriétaire) et, pour un enfant, par le filtre de la bibliothèque. Un enfant ne reçoit rien sur une œuvre qu'il ne voit pas, ni directement, ni par une série qui en contient une autre qu'il ne voit pas. La décision est reprise à la construction du message : une œuvre reclassée ou un plafond abaissé après l'événement suffit à retirer la ligne.
- **Réglages** : `GET` et `PUT /api/v1/me/notifications` (mode, heures calmes, fuseau) ; ils ne concernent que le profil connecté, et une clé d'API ou un jeton est refusé.

**Limite** : aucun profil n'a de destination à lui. Ces messages partent par les canaux de l'instance, que plusieurs personnes (un enfant compris) peuvent lire. **Ils ne contiennent donc aucun titre, aucun nom de série et aucun titre de demande**, pour personne : ils disent « N nouveaux chapitres », « N demandes arrivées », pour le nom du profil (jamais celui d'un enfant, dont le message est anonyme), avec un lien vers l'application quand `public_url` est réglé. Les titres en clair attendent une destination par profil (dette M4). Écart à la carte L14 : « Apprise vers le demandeur » n'est atteint que dans cette forme, attribuable par le nom du profil adulte, sans titre.

Aucune URL de canal, aucun titre et aucun texte d'erreur n'entre dans un journal ni dans le paquet de diagnostic. Un envoi en échec est retenté (5 min, 10 min, 20 min…) puis abandonné au bout de cinq essais. **Double envoi possible** : si le processus s'arrête entre l'envoi et l'effacement de la ligne, ou si un envoi dure plus que le bail de 5 minutes, le message repart une fois (livraison au moins une fois, jamais zéro). Une seule tâche tourne à la fois (clé de déduplication). `PUT /api/v1/me/notifications` remplace l'ensemble : le mode et le fuseau sont obligatoires, rien n'est remis à une valeur par défaut en silence.
