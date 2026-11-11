# Réveil à la demande avec Sablier

Si Sablier arrête Suwayomi (ou Kavita) quand ils sont inactifs, Shelfhold peut les réveiller
lui-même (EF-INT-022, [ADR-0025](../adr/0025-reveil-services-wakehook.md)). C'est facultatif : sans
réglage ci-dessous, Shelfhold ne réveille rien et Suwayomi doit rester toujours actif.

**Shelfhold lui-même ne doit jamais être derrière Sablier** : ses tâches périodiques ne
s'exécuteraient pas.

## Réglages

Variables d'environnement (ou clés de `config.toml`), détaillées dans
[la référence](../reference/config.md) :

| Variable | Rôle |
| --- | --- |
| `SHELFHOLD_WAKE_URL` | Adresse de l'API de Sablier, sur votre réseau (par exemple `http://sablier:10000`). |
| `SHELFHOLD_WAKE_SUWAYOMI` | Noms des conteneurs de Suwayomi, séparés par des virgules, ou `group:<nom>` seul. |
| `SHELFHOLD_WAKE_KAVITA` | Idem pour Kavita (facultatif, déconseillé : voir plus bas). |
| `SHELFHOLD_WAKE_TIMEOUT_S` | Combien de temps attendre qu'un outil soit prêt (60 s par défaut). Ce délai est distinct de tous les autres. |
| `SHELFHOLD_WAKE_SESSION_S` | Durée de session demandée à Sablier (300 s par défaut) ; Shelfhold la renouvelle toutes les moitiés de durée pendant un travail long. |
| `SHELFHOLD_WAKE_TOKEN` | Jeton d'un Sablier derrière un proxy qui authentifie, envoyé en `Authorization: Bearer`. Secret : jamais journalisé (`..._FILE` le lit dans un fichier). |

L'adresse de Sablier suit la même règle que celle de Suwayomi ou de Kavita : une adresse de votre
réseau local, sans identifiants ; les appels passent par le même contrôle de sortie (aucune
redirection vers une adresse privée que vous n'avez pas déclarée).

## Ce que fait Shelfhold

- **Suwayomi** est réveillé avant chaque lot et avant chaque vérification des nouveautés.
- En mode `piloted` (`shelfhold config set source.check_mode piloted`, défaut dès qu'un réveil est
  configuré), Shelfhold demande ensuite à Suwayomi de rafraîchir chaque série suivie, à rythme poli
  (2 s plus une gigue entre deux séries d'une même source amont), attend la fin, puis lit les
  nouveautés. En mode `always_on`, il réveille Suwayomi mais n'envoie aucun rafraîchissement.
- **Kavita**, s'il est derrière Sablier, est réveillé avant `scan-folder`, l'audit d'accès et la
  confirmation d'arrivée. Un Kavita qui ne se réveille pas à temps ne reçoit **aucune écriture** ;
  l'audit, lui, échoue en sécurité : les étagères sous 18+ restent en pause, un Kavita endormi ne
  lève jamais la barrière parentale.
- Un réveil qui échoue **n'est pas une panne** de la source : le lot est différé, rien n'est compté
  contre elle. Après trois lots différés de suite, l'événement `wake.deferred` vous prévient ;
  vérifiez Sablier et les noms de conteneurs, ou démarrez l'outil à la main.

## Sonde de santé après le réveil

Une fois Suwayomi réveillé, et avant de lire une série, Shelfhold lit la liste des sources de Suwayomi : une
source absente, remplacée par un bouchon ou à moitié mise à jour est déclarée non chargée (`source.not_loaded`),
sans rien compter contre elle. Il relance aussi, de façon bornée, les chapitres demandés mais jamais
téléchargés. Le détail est dans [suwayomi-multi-comptes.md](suwayomi-multi-comptes.md).

## Ce que Sablier ne change pas

- Un réveil qui échoue ne crée aucune candidate à la mise à l'abri, n'ouvre aucune pause et ne compte aucune
  erreur de contenu : il diffère le lot.
- Le défaut de `source.check_mode` est `always_on` ; il vaut `piloted` quand un réveil est configuré et
  qu'aucune valeur n'a été enregistrée (`shelfhold config set source.check_mode always_on` pour le refuser).
