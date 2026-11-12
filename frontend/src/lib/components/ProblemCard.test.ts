import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import { genericProblem, problemView } from '#lib/problems.ts';
import ErrorPage from '../../routes/+error.svelte';
import ProblemCard from './ProblemCard.svelte';

describe('ProblemCard (EF-UX-003)', () => {
  // req: EF-UX-003
  it('test_problem_card_shows_cause_and_action', () => {
    render(ProblemCard, {
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
    render(ProblemCard, { problem: problemView({ code: 'Not A Code', request_id: 'req-42' }) });
    expect(screen.getByText('Une erreur est survenue.')).toBeInTheDocument();
    expect(screen.getByText('req-42')).toBeInTheDocument();
    expect(screen.queryByText('Cause')).toBeNull();
    expect(screen.queryByText('Que faire')).toBeNull();
  });

  // req: EF-UX-003
  it('shows a text as text, never as markup', () => {
    render(ProblemCard, {
      problem: problemView({ code: 'x_y', title: '<img src=x onerror=alert(1)>', request_id: 'r' })
    });
    expect(document.querySelector('img')).toBeNull();
  });

  // req: EF-UX-003
  it('shows no request id when there is none', () => {
    render(ProblemCard, { problem: genericProblem() });
    expect(screen.queryByText('Identifiant de requête')).toBeNull();
  });

  // req: EF-UX-003
  it('the error screen has a level-one heading (the SPA fallback of a client route)', () => {
    render(ErrorPage);
    expect(screen.getByRole('heading', { level: 1, name: 'Une erreur est survenue.' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });
});
