# Plugins tiers

Un plugin tiers est du code qui n'a pas été écrit pour Shelfhold par ses mainteneurs. Il est
**désactivé par défaut** (EF-SRC-019, ENF-SEC-009). Cette page explique comment l'installer, l'activer
et ce que cela vous coûte.

## Ce que vous acceptez en l'activant

Un plugin activé tourne **dans le processus de Shelfhold, avec les mêmes droits que lui** : il lit la
base, le trousseau de clés, `/data`, la bibliothèque et l'environnement du processus, et il peut les
modifier ou les envoyer ailleurs. Aucun bac à sable n'existe avant le jalon M6. La confirmation
(« mêmes droits que Shelfhold ») sert à vous le faire dire.

Ce que la garde fait malgré cela :

- **Aucun secret n'est transmis.** Le chargeur appelle le plugin sans argument : ni configuration, ni
  clé, ni objet de l'hôte. Pendant l'import du plugin, les variables d'environnement dont le nom
  évoque un secret (`SECRET`, `PASSWORD`, `TOKEN`, `KEY`, `*_FILE`...) sont retirées, puis remises.
  C'est une précaution, pas une barrière : un code qui tourne dans le même processus peut lire
  `/proc/self/environ` (l'environnement d'origine du processus, que ce retrait ne change pas), sa
  mémoire et ses fichiers. Un thread lancé par le plugin pendant l'import relit `os.environ` une
  fois les variables remises. Ce que la garde garantit, c'est qu'aucun secret n'est *passé* au
  plugin ; elle ne l'empêche pas d'en aller chercher.
- **Il ne peut pas masquer l'hôte.** Les plugins sont installés dans leur propre `site-packages`
  (`/data/plugins/site-packages`), qui n'est **pas** dans `sys.path` : un chercheur placé tout à la
  fin de `sys.meta_path` ne résout que les modules de premier niveau des plugins activés, depuis
  leurs sources (jamais de bytecode lu ou écrit). `shelfhold`, la bibliothèque standard et les
  dépendances de l'hôte (`httpx2`, `starlette`...) se résolvent toujours vers l'hôte, et un
  plugin installé mais non confirmé reste introuvable, même par un import paresseux de l'hôte. À l'installation, un plugin dont le nom de distribution ou l'un des modules
  de premier niveau entre en conflit avec l'hôte est refusé, après normalisation PEP 503 (casse,
  `-`, `_` et `.` sont équivalents).
- **Aucun `.pth` n'est exécuté** : le dossier n'est jamais ajouté à `sys.path`, donc aucun `.pth` n'est lu, et une
  roue qui contient un `.pth` est refusée.
- **Un faux `shelfhold-*.dist-info` ne change rien** : les points d'entrée d'un plugin ne sont lus que
  dans son dossier, et seulement pour les distributions installées par `shelfhold plugin install`.
  Les métadonnées et les points d'entrée de l'hôte ne viennent jamais de ce dossier.

## Installer

L'installation se fait depuis un fichier `.whl` local et son empreinte sha256, sans réseau :

```sh
shelfhold plugin install ./mon_plugin-1.0-py3-none-any.whl --sha256 <64 chiffres hexadécimaux>
```

La commande lance `pip install --only-binary :all: --require-hashes --no-deps --no-index --target`
(ou `uv pip install` avec les mêmes options si `pip` n'est pas dans l'image). Aucune dépendance n'est
résolue : si le plugin en a besoin, ce sont celles de l'hôte. Une roue qui contient un `.pth`, un
dossier `.data`, un chemin sortant de l'archive, un `*.dist-info` ou `*.egg-info` qui n'est pas le
sien, `sitecustomize`/`usercustomize`, du bytecode ou un module compilé (`.so`) est refusée : seuls
les plugins en Python pur, en sources, sont acceptés.
Le code du plugin n'est pas exécuté à l'installation.

## Activer

1. Définissez `SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS=1` dans l'environnement de Shelfhold.
2. Lancez `shelfhold plugin enable <nom>` avec la variable définie, et tapez la phrase demandée :
   `mêmes droits que Shelfhold`.
3. Redémarrez Shelfhold : les plugins se chargent **une seule fois, au démarrage**.

Les deux conditions sont revérifiées à chaque démarrage. La variable seule ne suffit pas, la
confirmation seule non plus. La confirmation est liée à l'empreinte de tous les fichiers installés (sans exception, bytecode
compris), et un fichier que personne n'a installé dans le dossier annule toutes les confirmations ;
elle est liée à l'empreinte des fichiers installés : si les
fichiers changent, ou si le plugin est réinstallé, elle ne vaut plus et il faut la retaper.
`/data/plugins/state.json` est lisible par le seul compte de Shelfhold ; qui peut l'écrire peut déjà
tout faire dans `/data`.

Chaque installation, activation, désactivation, suppression et chaque refus est écrit dans le journal
d'audit (`plugin.install`, `plugin.enable`, `plugin.enable_refused`...). Si l'écriture de l'audit
échoue, rien n'est modifié.

## Vérifier

- `shelfhold plugin list` : les plugins installés et la raison pour laquelle ils sont inactifs.
- `shelfhold plugin list --load` : fait le chargement du démarrage et montre le résultat de chacun.
- `shelfhold plugin disable <nom>` retire la confirmation ; `shelfhold plugin remove <nom>` désinstalle.

## Entrées-sorties directes : un détecteur, pas une isolation

Un plugin ne fait jamais d'entrée-sortie lui-même : il reçoit le `Fetcher` de l'hôte (EF-SRC-002).
Pendant que son code tourne, y compris pendant son import au démarrage, un crochet d'audit Python
(`sys.addaudithook`) surveille l'ouverture d'une socket ou la résolution d'un nom, le lancement d'un
processus, d'un thread ou d'une bibliothèque native (`ctypes`), l'envoi d'un signal, et l'ouverture,
la liste, l'écriture ou la suppression d'un fichier hors du dossier de travail du plugin (aucun
dossier par défaut). Le plugin peut relire le code qu'il importe sans être signalé.

- **En production, Shelfhold journalise et ne bloque rien.** Chaque événement donne une ligne
  `plugin.forbidden_io` (avertissement, avec `plugin_id`, l'événement et le chemin ou la cible).
  Bloquer casserait des plugins légitimes (la lecture du fuseau horaire par
  `datetime.now().astimezone()`, un fichier temporaire) sans fermer les autres chemins qu'offre le
  processus. Si le détecteur ne sait pas juger un événement, il le journalise aussi.
- **Dans le kit de conformité du SDK**, le même crochet **refuse** l'événement avant qu'il ait lieu
  (`ForbiddenIOError`). Le plugin échoue au contrôle `no_direct_io`, même s'il intercepte l'erreur.

C'est un **détecteur, pas une isolation**. Il signale un plugin négligent ou une dépendance bavarde,
pas un plugin écrit pour nuire, qui peut passer par :

- un thread : le démarrage d'un thread est signalé, mais ce que fait ensuite un `threading.Thread`
  n'est pas surveillé, car il n'hérite pas du contexte du plugin ;
- un thread de l'hôte déjà ouvert (un exécuteur, par exemple), qui n'est pas surveillé non plus ;
- un objet de l'hôte qui fait l'entrée-sortie à sa place ;
- un descripteur de fichier ou une socket que l'hôte a déjà ouverts ;
- la mémoire du processus ;
- le détecteur lui-même, dont l'état est un objet Python.

La seule isolation est le bac à sable du jalon M6. Les conditions d'activation ci-dessus restent donc
entières.

## Domaines autorisés et adresse surchargée

Le `Fetcher` d'un plugin ne joint que les domaines de ses `DomainRule` (EF-SRC-003), à chaque saut, redirections
comprises, et avant toute résolution DNS. Une demande hors liste lève `DomainNotAllowed`, journalisée
(`plugin.domain_not_allowed`) avec `plugin_id` et l'adresse sans sa requête.

| Motif | Couvre | Ne couvre pas |
|---|---|---|
| `example.org` | `example.org` | `www.example.org`, `evilexample.org` |
| `.example.org` | `example.org` et tous les noms en dessous | `evilexample.org`, `example.org.evil` |
| `*.example.org` | tous les noms sous `example.org` | `example.org` lui-même, `example.org.evil` |

Le joker est toujours l'étiquette entière la plus à gauche, suivie d'au moins deux étiquettes :
`*.org`, `a*.example.org` ou `foo.*.example.org` sont refusés et le plugin n'est pas chargé.
Shelfhold n'a pas de liste des suffixes publics : `*.co.uk` est accepté, à vous de juger. Les noms
se comparent sous leur forme ASCII (punycode, IDNA 2008), sans tenir compte de la casse ni d'un
point final. Un caractère qui ressemble à un point, ou un punycode invalide, rend le motif invalide.
Une adresse IP ne se déclare qu'exactement.

Une règle qui porte `base_url_setting` peut être pointée ailleurs par ce réglage (votre propre
serveur, par exemple). L'adresse réglée **remplace** le domaine déclaré, par son nom exact, sans
joker. Chaque changement est journalisé (`plugin.base_url_override`, avertissement, adresse
masquée comme dans les autres journaux) ; le retour au domaine déclaré aussi. Une valeur qui n'est
pas une adresse `http` ou `https` simple (identifiants `user:mot-de-passe@` compris) est refusée
(`plugin.base_url_override_refused`) et la règle ne joint plus rien. La garde contre les requêtes
internes s'applique toujours : une adresse surchargée ne rend pas joignable un service du réseau
local.

## Coller une adresse

Une adresse collée est confiée au premier plugin actif qui déclare `URL_RESOLVE` et dont un motif
`url_patterns` correspond (EF-SRC-004). Un motif s'écrit `<schéma>://<hôte>[<chemin>]` : schéma
`http`, `https` ou `*` ; hôte selon la grammaire des domaines ci-dessus ; dans le chemin, `*` vaut un
segment et `**` un nombre quelconque. Une adresse avec identifiants ou sur un port inhabituel ne
correspond à rien. Sans plugin qui correspond, ou si celui qui correspond ne trouve rien, la
réponse est 422 `source.no_match` (« aucune source »).

## Licence

Un plugin chargé en processus relève de l'exception de plugin provisoire (ENF-LEG-010). Tant qu'elle
n'est pas relue, préférez une licence compatible AGPL.
