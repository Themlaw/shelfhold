# Politique d'IA

## Qui écrit le code

Le code de Shelfhold est **majoritairement écrit par des agents d'IA orchestrés par le mainteneur**. Ce n'est pas un secret : c'est la méthode du projet, et elle s'accompagne de garde-fous.

## Garde-fous

1. **TDD obligatoire** : le test qui échoue d'abord, montré, puis le code minimal ([`docs/spec/03-roadmap-tdd.md`](docs/spec/03-roadmap-tdd.md) §4).
2. **Relecture croisée** : chaque diff produit par un agent est relu par un autre agent avant fusion.
3. **CI verte** : `ci-success` est requis par le ruleset de `main` ; aucune fusion sans.
4. **Le mainteneur relit à chaque jalon** et assume la responsabilité du résultat ; il valide seul les déploiements réels.
5. Aucun test modifié ou supprimé pour passer sans le signaler ; aucune régénération de snapshots ou de cassettes sans demande explicite, jamais en CI.
6. Les frontières de modules (import-linter) sont respectées ; pas de nouvelle dépendance sans justification dans la PR.
7. La vitesse ne prime jamais sur les garanties ni sur les tests rouges d'abord.

## Contributeurs extérieurs

Le code assisté par IA est accepté si vous le comprenez, l'avez relu et pouvez l'accepter sous la licence du projet (DCO, voir [CONTRIBUTING.md](CONTRIBUTING.md)). Pas de PR autonome d'agent : un humain assume chaque changement.

## Shelfhold n'embarque aucun modèle d'IA

Le produit n'inclut, n'appelle ni n'héberge aucun modèle d'IA. Si une intégration (par exemple un serveur MCP) arrive un jour, cette politique sera revue (AI Act).

Consignes détaillées pour les agents : [AGENTS.md](AGENTS.md).
