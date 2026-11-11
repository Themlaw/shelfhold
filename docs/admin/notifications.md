# Notifications d'exploitation (Apprise)

Shelfhold prévient l'administrateur quand quelque chose demande son attention : sauvegarde en échec, disque plein, destination injoignable, tâche abandonnée, secret illisible, source cassée, etc. (la liste est celle des événements d'exploitation, EF-INT-006). Les notifications passent par [Apprise](https://github.com/caronc/apprise), version 2.1 ou plus récente, moins de 3.

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
