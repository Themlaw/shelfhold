import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import ProblemCard from './ProblemCard.svelte';

describe('ProblemCard (EF-UX-003)', () => {
  it('test_problem_card_shows_cause_and_action', () => {
    render(ProblemCard, {
      props: {
        problem: {
          code: 'not_found',
          title: 'Page introuvable',
          cause: 'L’adresse demandée n’existe pas.',
          action: 'Vérifiez l’adresse.'
        }
      }
    });
    expect(screen.getByText('not_found')).toBeInTheDocument();
    expect(screen.getByText('Page introuvable')).toBeInTheDocument();
    expect(screen.getByText('L’adresse demandée n’existe pas.')).toBeInTheDocument();
    expect(screen.getByText('Vérifiez l’adresse.')).toBeInTheDocument();
  });

  it('test_unknown_error_code_shows_request_id', () => {
    render(ProblemCard, {
      props: {
        problem: {
          // no recognised stable code: show a generic text and the request identifier
          code: 'totally_unknown_code',
          title: undefined,
          request_id: 'req-42'
        }
      }
    });
    expect(screen.getByText('Une erreur est survenue.')).toBeInTheDocument();
    expect(screen.getByText('req-42')).toBeInTheDocument();
  });
});
