# Runners auto-hébergés de la CI

Les jobs lourds de `ci.yml` (`lint`, `backend-unit`, `backend-integration`, `backend-suites` et `coverage`) peuvent
tourner sur des runners auto-hébergés au lieu de l'image `ubuntu-24.04` de GitHub. Les checks, leurs noms et
`ci-success` ne changent pas : seul l'endroit où le job s'exécute change.

## Règle de sécurité

Le job `changes` calcule la sortie `runner`, que les cinq jobs utilisent comme `runs-on`. Elle ne désigne les
runners auto-hébergés (labels `self-hosted`, `linux`, `x64`, `shelfhold-ci`) **que si toutes ces conditions sont
réunies** :

1. la variable de dépôt `SELF_HOSTED` vaut `on` ;
2. l'événement est un `push` sur `main`, ou une `pull_request` dont la branche est **dans ce dépôt**
   (`head.repo.full_name == github.repository`) ;
3. ni l'auteur de la PR, ni `github.actor`, ni `github.triggering_actor` n'est `dependabot[bot]`.

Tout le reste reste sur `ubuntu-24.04` : PR d'un fork, PR de Dependabot (même relancée par un mainteneur),
`pull_request_target`, `workflow_dispatch`, `schedule`, variable absente ou à `off`. Le code d'un inconnu ne
s'exécute donc jamais sur une machine du projet. Les PR de forks demandent en plus une approbation pour tout
contributeur extérieur (réglage « Require approval for all outside collaborators »).

Ces cinq jobs ne reçoivent aucun secret du dépôt. Seul le jeton automatique en lecture (`github.token`, droits
`contents: read`) sert à l'audit en ligne de `zizmor`. `test_ci_workflows.py` vérifie la règle (fork, Dependabot,
autres événements, variable coupée), la liste des jobs concernés et l'absence de `secrets.` dans ces jobs.

## Ce qu'un runner doit garantir

- **Éphémère.** Un runner sert un seul job : il est enregistré avec une configuration JIT
  (`generate-jitconfig`), puis détruit avec son conteneur à la fin du job. Chaque job part d'un état propre.
- **Isolé.** Le conteneur tourne sous un utilisateur dédié, sans `sudo`, sans clé SSH ni jeton GitHub, avec un
  Docker *rootless* à lui. Ses connexions sortantes vers les réseaux privés, le lien local et la machine
  elle-même sont refusées : il n'atteint qu'Internet.
- **Équivalent à `ubuntu-24.04`.** L'image du job part d'Ubuntu 24.04 et contient ce que les jobs attendent
  (`git`, `jq`, `curl`, `zstd`, `python3`, `openssl`, `unshare`, une chaîne de compilation). `setup-uv`,
  `setup-node` et `actions/cache` y fonctionnent comme sur GitHub.
- **Labels génériques.** `self-hosted`, `linux`, `x64` et `shelfhold-ci`, sans rien qui décrive la machine.

e2e, l'image, WebKit et le banc de contrat restent sur GitHub : ils demandent Docker ou des navigateurs que
l'image du runner n'a pas.

## Activer, couper

```sh
gh variable set SELF_HOSTED --repo Themlaw/shelfhold --body on    # activer
gh variable set SELF_HOSTED --repo Themlaw/shelfhold --body off   # couper : tout repart sur GitHub
```

La variable est lue au début de chaque run, dans le job `changes`. Une fois coupée, les runs déjà lancés gardent
leur choix, et les suivants vont sur GitHub. Il n'y a rien d'autre à faire : les runners restés enregistrés
attendent sans recevoir de job.

Sans runner en ligne, un job envoyé sur `shelfhold-ci` attend dans la file. Si les runners sont arrêtés,
coupe d'abord la variable.

## Ajouter un runner

1. Sur la machine, crée un utilisateur dédié sans groupe ni `sudo`, avec Docker rootless, et un filtre de sortie
   qui ne laisse passer qu'Internet.
2. Construis l'image du job : Ubuntu 24.04, les outils ci-dessus et le runner GitHub Actions, dont la somme
   SHA-256 est vérifiée.
3. Pour chaque job, demande une configuration JIT au nom du dépôt :

   ```sh
   gh api -X POST repos/Themlaw/shelfhold/actions/runners/generate-jitconfig \
     -f name=shelfhold-ci-<n> -F runner_group_id=1 -f work_folder=_work \
     -f 'labels[]=self-hosted' -f 'labels[]=linux' -f 'labels[]=x64' -f 'labels[]=shelfhold-ci' \
     --jq .encoded_jit_config
   ```

   Transmets-la au conteneur sans l'afficher ni l'écrire (entrée standard, puis variable
   `ACTIONS_RUNNER_INPUT_JITCONFIG`), puis lance `./run.sh --jitconfig …` dans un conteneur `--rm`.
4. Recommence dès que le conteneur s'arrête. Le runner apparaît dans *Settings → Actions → Runners* le temps
   d'un job.

La configuration JIT ne sert qu'une fois. Le jeton qui l'obtient (droit d'administration du dépôt) reste hors des
machines de CI.

## Retirer un runner

Arrête la boucle qui le relance, puis supprime l'utilisateur dédié et son filtre. Supprime enfin les runners
hors ligne qui restent (`gh api -X DELETE repos/Themlaw/shelfhold/actions/runners/<id>`).
