# Banc de contrat Suwayomi (spike S5)

Piloté par `backend/tests/contract/suwayomi/` (`cd backend && uv run pytest -m contract tests/contract`, Docker requis).
Workflow : `.github/workflows/contract.yml` (manuel, lundi 04:17 UTC, PR touchant le banc ; non requis).

Le workflow n'ouvre pas d'issue (permissions minimales, `contents: read`) : un échec du cron se voit dans l'onglet
Actions, dont le porteur relit l'état chaque semaine. L'image est épinglée par digest ; la mise à jour passe par une PR
revue (stable d'au moins 14 jours, ADR-0016).
