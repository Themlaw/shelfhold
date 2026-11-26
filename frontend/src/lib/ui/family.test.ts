import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Segmented from './atoms/Segmented.svelte';
import AgeScale from './molecules/AgeScale.svelte';
import SecretOnce from './molecules/SecretOnce.svelte';
import { dismiss } from './toast.svelte.ts';

afterEach(() => dismiss());

describe('Segmented', () => {
  const options = [
    { value: 'add_directly', label: 'Ajouter directement' },
    { value: 'request', label: 'Demander' },
    { value: 'forbidden', label: 'Interdit' }
  ];

  // req: ENF-A11Y-002
  it('is one radio group: the chosen segment is checked and a click reports the new value', async () => {
    const seen: string[] = [];
    render(Segmented, { name: 'p', legend: 'Politique', options, value: 'request', onchange: (v: string) => seen.push(v) });
    expect(screen.getByRole('radio', { name: 'Demander' })).toBeChecked();
    await userEvent.click(screen.getByRole('radio', { name: 'Interdit' }));
    expect(seen).toEqual(['forbidden']);
    expect(screen.getByRole('radio', { name: 'Interdit' })).toBeChecked();
  });

  // req: ENF-A11Y-002
  it('moves inside the group with the arrow keys, as native radios do', async () => {
    render(Segmented, { name: 'p', legend: 'Politique', options, value: 'add_directly' });
    screen.getByRole('radio', { name: 'Ajouter directement' }).focus();
    await userEvent.keyboard('{ArrowRight}');
    expect(screen.getByRole('radio', { name: 'Demander' })).toBeChecked();
  });
});

describe('AgeScale', () => {
  const steps = [
    { value: 0, label: 'Tous' },
    { value: 10, label: '10+' },
    { value: 13, label: '13+' },
    { value: null, label: 'Aucun plafond' }
  ];

  // req: ENF-A11Y-002
  it('checks the step of the ceiling and reports the chosen one, a null ceiling included', async () => {
    const seen: Array<number | null> = [];
    render(AgeScale, { name: 'a', legend: 'Âge maximal', steps, value: 10, onchange: (v: number | null) => seen.push(v) });
    expect(screen.getByRole('radio', { name: '10+' })).toBeChecked();
    await userEvent.click(screen.getByRole('radio', { name: '13+' }));
    await userEvent.click(screen.getByRole('radio', { name: 'Aucun plafond' }));
    expect(seen).toEqual([13, null]);
  });

  // req: ENF-A11Y-002
  it('prints the number of works of a step when the server gives it', () => {
    render(AgeScale, { name: 'a', legend: 'Âge maximal', steps, value: 10, counts: { '0': 398, '10': 642 } });
    expect(screen.getByText('642')).toBeInTheDocument();
    expect(screen.queryByText('1 284')).toBeNull();
  });
});

describe('SecretOnce', () => {
  const fields = [
    { label: 'Identifiant', value: 'abcd2345' },
    { label: 'Secret', value: 'S3CR3T', secret: true, testid: 'secret' }
  ];

  // req: ENF-A11Y-001
  it('shows the values once, copies one on request and closes on the one gesture', async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: write }, configurable: true });
    const onack = vi.fn();
    render(SecretOnce, { title: 'Notez ce secret', note: 'Il ne sera plus affiché.', fields, ackLabel: 'J’ai noté', onack });
    expect(screen.getByRole('heading', { name: 'Notez ce secret' })).toBeInTheDocument();
    expect(screen.getByTestId('secret')).toHaveTextContent('S3CR3T');
    await userEvent.click(screen.getByRole('button', { name: /Copier.*Secret/ }));
    await waitFor(() => expect(write).toHaveBeenCalledWith('S3CR3T'));
    await userEvent.click(screen.getByRole('button', { name: 'J’ai noté' }));
    expect(onack).toHaveBeenCalledOnce();
  });
});
