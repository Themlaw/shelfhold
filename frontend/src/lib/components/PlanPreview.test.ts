import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import PlanPreview from './PlanPreview.svelte';

// The preview of a rename plan (EF-EXP-030, EF-EXP-037): the steps, the batches, what is blocked,
// and, before Kavita readers lose their progress, the figure and a phrase to type. The server
// counts; the component only shows the figure and gates the button. Its phrase field is a
// TypedConfirm whose button is « Appliquer le plan » (`confirmLabel`) and which has no « Annuler »
// of its own (`oncancel` not given): the plan has one Cancel, one Apply.
const PHRASE = 'PERDRE LA PROGRESSION';
const step = (src: string, dst: string, id: string) => ({ kind: 'file', src, dst, export_item_id: id });
const plan = (over: Record<string, unknown> = {}) => ({
  plan_id: '01JPLAN0000000000000000R01',
  base_version: '4242',
  steps: [step('A/a.cbz', 'A/b.cbz', '01JITEM00000000000000000A1'), step('B/c.cbz', 'B/d.cbz', '01JITEM00000000000000000B1')],
  batches: 2,
  blocked: [],
  warnings: [{ code: 'progress_loss', readers: 7, exact: true, series: ['A', 'B'] }],
  ...over
});
const setup = (over: Record<string, unknown> = {}) => {
  const onapply = vi.fn();
  const oncancel = vi.fn();
  render(PlanPreview, { plan: plan(over), onapply, oncancel } as never);
  return { onapply, oncancel, user: userEvent.setup() };
};
const apply = () => screen.getByRole('button', { name: 'Appliquer le plan' });

describe('PlanPreview', () => {
  // req: EF-EXP-030
  it('lists every rename, source then destination, and the number of batches', () => {
    setup();
    const steps = within(screen.getByRole('list', { name: 'Renommages' })).getAllByRole('listitem');
    expect(steps).toHaveLength(2);
    expect(steps[0]).toHaveTextContent('A/a.cbz → A/b.cbz');
    expect(steps[1]).toHaveTextContent('B/c.cbz → B/d.cbz');
    expect(screen.getByText('2 renommages en 2 lots')).toBeTruthy();
  });

  // req: EF-EXP-037
  it('test_rename_plan_preview_shows_reader_count_warning', () => {
    setup();
    const warning = screen.getByRole('alert');
    expect(warning).toHaveTextContent('La progression Kavita de 7 lecteurs sera perdue.');
  });

  // req: EF-EXP-037
  it('says "at least" when the count is not exact', () => {
    setup({ warnings: [{ code: 'progress_loss', readers: 7, exact: false, series: ['A'] }] });
    expect(screen.getByRole('alert')).toHaveTextContent("La progression Kavita d'au moins 7 lecteurs sera perdue.");
  });

  // req: EF-EXP-037
  it('says "one reader" in the singular', () => {
    setup({ warnings: [{ code: 'progress_loss', readers: 1, exact: true, series: ['A'] }] });
    expect(screen.getByRole('alert')).toHaveTextContent('La progression Kavita de 1 lecteur sera perdue.');
  });

  // req: EF-EXP-037
  it('keeps the button disabled until the phrase is typed exactly, then applies with the confirmation', async () => {
    const { onapply, user } = setup();
    expect(apply()).toBeDisabled();
    const field = screen.getByLabelText('Recopiez la phrase pour confirmer');
    expect(screen.getByText(PHRASE)).toBeTruthy();
    for (const wrong of ['perdre la progression', `${PHRASE} `, ` ${PHRASE}`, 'PERDRE  LA PROGRESSION']) {
      await user.clear(field);
      await user.type(field, wrong);
      expect(apply(), wrong).toBeDisabled();
    }
    await user.clear(field);
    await user.type(field, PHRASE);
    expect(apply()).toBeEnabled();
    await user.click(apply());
    expect(onapply).toHaveBeenCalledTimes(1);
    expect(onapply).toHaveBeenCalledWith({ confirmProgressLoss: true });
  });

  // req: EF-EXP-037
  it('never applies by itself and not on a wrong phrase', async () => {
    const { onapply, user } = setup();
    await user.type(screen.getByLabelText('Recopiez la phrase pour confirmer'), 'perdre{Enter}');
    expect(onapply).not.toHaveBeenCalled();
  });

  // req: EF-EXP-030
  it('asks for no phrase when nobody would lose anything, and applies without the confirmation', async () => {
    const { onapply, user } = setup({ warnings: [] });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByLabelText('Recopiez la phrase pour confirmer')).toBeNull();
    expect(apply()).toBeEnabled();
    await user.click(apply());
    expect(onapply).toHaveBeenCalledWith({ confirmProgressLoss: false });
  });

  // req: EF-EXP-030
  it('shows what is blocked, by count', () => {
    setup({ blocked: [{ code: 'path_taken', src: 'A/a.cbz' }, { code: 'path_taken', src: 'B/c.cbz' }] });
    expect(screen.getByText('2 renommages bloqués')).toBeTruthy();
  });

  // req: EF-EXP-030
  it('offers nothing to apply when there is nothing to rename (no plan was stored)', () => {
    setup({ plan_id: null, steps: [], batches: 0, warnings: [] });
    expect(screen.getByText('Rien à renommer.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Appliquer le plan' })).toBeNull();
  });

  // req: EF-EXP-030
  it('cancels without applying', async () => {
    const { onapply, oncancel, user } = setup();
    await user.click(screen.getByRole('button', { name: 'Annuler' }));
    expect(oncancel).toHaveBeenCalledTimes(1);
    expect(onapply).not.toHaveBeenCalled();
  });
});
