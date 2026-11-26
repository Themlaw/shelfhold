import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import { genericProblem, problemView } from '#lib/problems.ts';
import ErrorPage from '../../../routes/+error.svelte';
import ProblemAlert, { refusalOf } from './ProblemAlert.svelte';

describe('ProblemAlert, as the one place a problem is told (EF-UX-003)', () => {
  // req: EF-UX-003
  it('test_problem_alert_shows_cause_and_action', () => {
    render(ProblemAlert, {
      problem: problemView({
        code: 'not_found',
        title: 'Page introuvable.',
        cause: 'L’élément n’existe pas.',
        action: 'Vérifiez le lien.',
        request_id: 'req-1'
      })
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Page introuvable.');
    expect(screen.getByText('not_found')).toBeInTheDocument();
    expect(screen.getByText('L’élément n’existe pas.')).toBeInTheDocument();
    expect(screen.getByText('Vérifiez le lien.')).toBeInTheDocument();
    expect(screen.getByText('req-1')).toBeInTheDocument();
  });

  // req: EF-UX-003
  it('test_unknown_error_code_shows_request_id', () => {
    render(ProblemAlert, { problem: problemView({ code: 'Not A Code', request_id: 'req-42' }) });
    expect(screen.getByText('Une erreur est survenue.')).toBeInTheDocument();
    expect(screen.getByText('req-42')).toBeInTheDocument();
  });

  // req: EF-UX-003
  it('shows a text as text, never as markup', () => {
    render(ProblemAlert, {
      problem: problemView({ code: 'x_y', title: '<img src=x onerror=alert(1)>', request_id: 'r' })
    });
    expect(document.querySelector('img')).toBeNull();
  });

  // req: EF-UX-003
  it('puts the sentence of the screen before what the server said', () => {
    render(ProblemAlert, {
      problem: problemView({ code: 'x_y', title: 'Titre', cause: 'La cause', request_id: 'r' }),
      note: 'Rien n’a été modifié.'
    });
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Rien n’a été modifié.');
    expect(alert.textContent!.indexOf('Rien n’a été modifié.')).toBeLessThan(alert.textContent!.indexOf('La cause'));
  });

  // req: EF-UX-003
  it('shows no request id when there is none', () => {
    render(ProblemAlert, { problem: genericProblem() });
    expect(screen.queryByText(/Identifiant/)).toBeNull();
  });

  // req: EF-UX-003
  it('the error screen has a level-one heading (the SPA fallback of a client route)', () => {
    render(ErrorPage);
    // the kind of the failure decides the words (ErrorScreen); never the raw message, never JSON
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('main')).not.toHaveTextContent(/[{}]|Internal Error|Not Found/);
  });
});

describe('ProblemAlert, the one way to tell a refusal (EF-UX-003)', () => {
  // req: EF-UX-003
  it('tells a 403 asked as a block as the locked volume, without the raw code', () => {
    const problem = problemView({ code: 'forbidden', title: 'Accès refusé', status: 403, request_id: 'req-9' });
    render(ProblemAlert, { problem, kind: refusalOf(problem) });
    expect(screen.getByRole('heading', { level: 2, name: /hors de votre portée/ })).toBeInTheDocument();
    expect(screen.queryByText('forbidden')).toBeNull();
    expect(screen.getByRole('link', { name: 'Revenir à l’accueil' })).toBeInTheDocument();
  });

  // req: EF-UX-003
  it('sends a 401 to the sign-in page, asking to come back', () => {
    const problem = problemView({ code: 'unauthenticated', title: 'Session', status: 401 });
    render(ProblemAlert, { problem, kind: refusalOf(problem) });
    expect(screen.getByRole('link', { name: 'Se connecter' }).getAttribute('href')).toMatch(/^\/login(\?next=.*)?$/);
  });

  // req: EF-UX-003
  it('keeps a refusal not asked as a block as an alert', () => {
    render(ProblemAlert, { problem: problemView({ code: 'forbidden', title: 'Accès refusé', status: 403 }) });
    expect(screen.getByRole('alert')).toHaveTextContent('Accès refusé');
  });
});
