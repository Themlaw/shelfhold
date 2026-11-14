import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import TypedConfirm from './TypedConfirm.svelte';

// The typed confirmation (EF-EXP-031, EF-EXP-037): the server gives the phrase, the person copies
// it, and the button stays dead until what was typed is that phrase, letter for letter.
const PHRASE = 'RETIRER manga/';
const setup = () => {
  const onconfirm = vi.fn();
  const oncancel = vi.fn();
  render(TypedConfirm, { phrase: PHRASE, onconfirm, oncancel } as never);
  return { onconfirm, oncancel, user: userEvent.setup() };
};
const input = () => screen.getByLabelText('Recopiez la phrase pour confirmer');
const confirm = () => screen.getByRole('button', { name: 'Confirmer' });

describe('TypedConfirm', () => {
  // req: EF-EXP-031
  it('shows the phrase to copy and keeps the button disabled while nothing is typed', () => {
    setup();
    expect(screen.getByText(PHRASE)).toBeTruthy();
    expect(confirm()).toBeDisabled();
  });

  // req: EF-EXP-031
  it('enables the button for the exact phrase and hands the typed text over', async () => {
    const { onconfirm, user } = setup();
    await user.type(input(), PHRASE);
    expect(confirm()).toBeEnabled();
    await user.click(confirm());
    expect(onconfirm).toHaveBeenCalledTimes(1);
    expect(onconfirm).toHaveBeenCalledWith(PHRASE);
  });

  // req: EF-EXP-031
  it.each([
    ['another case', 'retirer manga/'],
    ['a leading space', ` ${PHRASE}`],
    ['a trailing space', `${PHRASE} `],
    ['a doubled space', 'RETIRER  manga/'],
    ['a missing letter', 'RETIRER manga'],
    ['an extra letter', `${PHRASE}x`]
  ])('keeps the button disabled for %s', async (_why, typed) => {
    const { onconfirm, user } = setup();
    await user.type(input(), typed);
    expect(confirm()).toBeDisabled();
    await user.click(confirm());
    expect(onconfirm).not.toHaveBeenCalled();
  });

  // req: EF-EXP-031
  it('does not confirm with the Enter key while the phrase is wrong', async () => {
    const { onconfirm, user } = setup();
    await user.type(input(), 'retirer{Enter}');
    expect(onconfirm).not.toHaveBeenCalled();
  });

  // req: EF-EXP-031
  it('disables the button again when the phrase is spoiled after being right', async () => {
    const { user } = setup();
    await user.type(input(), PHRASE);
    expect(confirm()).toBeEnabled();
    await user.type(input(), '!');
    expect(confirm()).toBeDisabled();
  });

  // req: EF-EXP-031
  it('cancels without confirming', async () => {
    const { onconfirm, oncancel, user } = setup();
    await user.type(input(), PHRASE);
    await user.click(screen.getByRole('button', { name: 'Annuler' }));
    expect(oncancel).toHaveBeenCalledTimes(1);
    expect(onconfirm).not.toHaveBeenCalled();
  });

  // req: EF-EXP-031
  it('is a plain text field: nothing is filled in for the person', () => {
    setup();
    expect(input()).toHaveAttribute('autocomplete', 'off');
    expect((input() as HTMLInputElement).value).toBe('');
  });
});
