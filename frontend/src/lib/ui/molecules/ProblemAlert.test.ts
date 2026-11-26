import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import { genericProblem, problemView } from '#lib/problems.ts';
import ErrorPage from '../../../routes/+error.svelte';
import ProblemAlert from './ProblemAlert.svelte';

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
