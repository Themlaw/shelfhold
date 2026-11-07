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

## Licence

Un plugin chargé en processus relève de l'exception de plugin provisoire (ENF-LEG-010). Tant qu'elle
n'est pas relue, préférez une licence compatible AGPL.
