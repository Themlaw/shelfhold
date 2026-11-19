# Runners auto-hébergés de la CI

Les jobs lourds de `ci.yml` (`lint`, `backend-unit`, `backend-integration`, `backend-suites` et `coverage`) peuvent
tourner sur des runners auto-hébergés au lieu de l'image `ubuntu-24.04` de GitHub. Les checks, leurs noms et
`ci-success` ne changent pas : seul l'endroit où le job s'exécute change.

## Règle de sécurité

**Le `runs-on` n'est pas une protection.** Sur une `pull_request`, GitHub exécute le `ci.yml` de la PR elle-même.
Une PR venue d'un fork peut donc réécrire son `runs-on`, ou ajouter un workflow, pour demander n'importe quel
label. La protection repose sur deux verrous.

1. **Aucun runner n'attend.** Aucun runner n'est enregistré à l'avance sous un label générique. Pour chaque job
   en file qui demande un runner auto-hébergé, le contrôleur des runners lit son run par l'API, sur des champs que
   la PR ne contrôle pas :
   - `head_repository` est ce dépôt, pas un fork ;
   - l'événement est un `push` sur `main` ou une `pull_request` ;
   - le workflow est `.github/workflows/ci.yml` ;
   - ni `actor` ni `triggering_actor` n'est `dependabot[bot]`, et la branche n'est pas une branche `dependabot/` ;
   - le job demande exactement le label `run-<id de son run>`.

   Si tout est bon, et seulement alors, le contrôleur crée un runner éphémère qui porte ce seul label et ne sert
   qu'à ce job. Si un job d'un run non fiable demande un tel runner, le contrôleur ne crée plus rien et arrête
   les runners inactifs tant que ce job reste en file.
2. **Routage dans `ci.yml`.** Le job `changes` calcule la sortie `runner`, que les cinq jobs utilisent comme
   `runs-on`. Elle vaut `run-<github.run_id>` seulement si la variable de dépôt `SELF_HOSTED` vaut `on` et si le
   code vient du dépôt, selon les mêmes critères. Sinon, elle vaut `ubuntu-24.04` : forks, Dependabot (même relancé
   par un mainteneur), `pull_request_target`, `workflow_dispatch`, `schedule`, variable absente ou à `off`.

**Ne jamais approuver le run d'un fork qui touche `.github/`.** Le dépôt exige une approbation pour les workflows
de tout contributeur extérieur. Ce clic est la dernière barrière humaine : avant d'approuver, lis le diff.

Ces cinq jobs ne reçoivent aucun secret du dépôt. Seul le jeton automatique en lecture (`github.token`, droits
`contents: read`) sert à l'audit en ligne de `zizmor`. `test_ci_workflows.py` vérifie :
- la règle de routage (fork, Dependabot, autres événements, variable coupée) ;
- la forme du `runs-on` (le seul label du run, jamais `self-hosted`) ;
- la liste des jobs concernés, et l'absence de `secrets.` dans ces jobs.

## Ce qu'un runner doit garantir

- **Éphémère.** Le contrôleur crée le runner pour un seul job, avec une configuration JIT
  (`generate-jitconfig`). Le runner est détruit avec son conteneur à la fin du job, et chaque job part d'un état
  propre.
- **Isolé.**
  - Le conteneur tourne sous un utilisateur dédié, sans `sudo`, sans clé SSH ni jeton GitHub, avec un Docker
    *rootless* à lui.
  - Chaque emplacement a son propre réseau.
  - Les connexions sortantes vers les réseaux privés, le lien local et la machine elle-même sont refusées : il
    n'atteint qu'Internet.
  - Les écritures des jobs tiennent dans un disque de taille fixe.
- **Équivalent à `ubuntu-24.04`.** L'image du job part d'Ubuntu 24.04 et contient ce que les jobs attendent
  (`git`, `jq`, `curl`, `zstd`, `python3`, `openssl`, `unshare`, une chaîne de compilation). `setup-uv`,
  `setup-node` et `actions/cache` y fonctionnent comme sur GitHub.
- **Secret hors de vue.** La configuration JIT passe par l'entrée standard, puis par la variable
  `ACTIONS_RUNNER_INPUT_JITCONFIG`, que le runner lit puis efface. Elle n'apparaît jamais sur une ligne de commande.

e2e, l'image, WebKit et le banc de contrat restent sur GitHub : ils demandent Docker ou des navigateurs que
l'image du runner n'a pas.

## Activer, couper

```sh
gh variable set SELF_HOSTED --repo Themlaw/shelfhold --body on    # activer
gh variable set SELF_HOSTED --repo Themlaw/shelfhold --body off   # couper : tout repart sur GitHub
```

La variable est lue au début de chaque run, dans le job `changes`. Une fois coupée, les runs déjà lancés gardent
leur choix, et les suivants vont sur GitHub.

N'active la variable que si le contrôleur tourne et que les machines répondent. Sinon, les jobs envoyés sur
`run-<id>` attendent dans la file jusqu'à 24 h, et `ci-success` avec eux. Pour tout couper, passe la variable à
`off`, puis arrête le contrôleur et les conteneurs.

## Ajouter un runner

1. Sur la machine, crée un utilisateur dédié sans groupe ni `sudo`, avec Docker rootless, un réseau par
   emplacement et un disque de taille fixe pour ses données. Ajoute un filtre de sortie qui ne laisse passer
   qu'Internet.
2. Construis l'image du job : Ubuntu 24.04, les outils ci-dessus et le runner GitHub Actions, dont la somme
   SHA-256 est vérifiée.
3. Le contrôleur sert la machine. Pour chaque job en file dont le run passe le contrôle, il demande une
   configuration JIT avec le seul label `run-<id du run>` :

   ```sh
   gh api -X POST repos/Themlaw/shelfhold/actions/runners/generate-jitconfig \
     -f name=shelfhold-ci-<n> -F runner_group_id=1 -f work_folder=_work -f 'labels[]=run-<id>' \
     --jq .encoded_jit_config
   ```

   Il la transmet au conteneur sans l'afficher ni l'écrire, puis lance le conteneur avec `--rm`.
4. Le jeton du contrôleur est un jeton à grain fin limité à ce dépôt (Administration en écriture, Actions en
   lecture). Il reste hors des machines de CI.

## Retirer un runner

Arrête le contrôleur, puis supprime l'utilisateur dédié, son disque et son filtre. Supprime enfin les runners hors
ligne qui restent (`gh api -X DELETE repos/Themlaw/shelfhold/actions/runners/<id>`).
