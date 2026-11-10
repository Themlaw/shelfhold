## Résumé

<!-- Quoi et pourquoi. Pas de nom de site ni de lien vers du contenu non autorisé. -->

## Exigences concernées

<!-- EF-… / ENF-… (voir docs/spec/01-cahier-des-charges.md) -->

## Liste de contrôle

- [ ] Le test rouge a été écrit d'abord (et vu échouer pour la bonne raison)
- [ ] Aucun test modifié ou supprimé pour passer (sinon : expliqué ci-dessus)
- [ ] Tous les commits sont signés DCO (`git commit -s`)
- [ ] Fixtures synthétiques ou du domaine public uniquement
- [ ] Documentation mise à jour
- [ ] Migration de base : non / oui (réversible : oui / non)
- [ ] Nouvelle dépendance critique : inscrite dans `docs/dependencies.md`
- [ ] Si le code est écrit par un agent : relu par un autre agent ; CI verte

## Admission d'une source (si la PR ajoute une source)

- [ ] Preuve d'autorisation ou licence libre
- [ ] Conditions et robots lus (`terms_url`), limites déclarées
- [ ] Revue CODEOWNERS

## Admission d'un plugin de métadonnées (si la PR ajoute un MetadataProvider)

- [ ] Preuve d'autorisation ou licence libre de l'API et des données (ENF-LEG-003)
- [ ] Conditions lues : `terms_url` renseigné dans le manifeste, robots lus et respectés
- [ ] Limites déclarées : `quota` au manifeste (débit, plafond journalier), attribution et mention de la source dans la provenance
- [ ] Aucune clé ni secret dans le dépôt ; `auth` déclaré si la source en exige une (par utilisateur ou par instance)
- [ ] Le kit de conformité (`run_metadata_conformance`) passe, hors réseau, sur des fixtures synthétiques
- [ ] Revue CODEOWNERS
