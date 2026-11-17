// The preview of an adoption (M4 L36): it probes every archive of the library, which takes
// minutes for a large one, so the server answers 200 with the report when it is quick and 202
// with a task otherwise. Both screens that preview go through here.
import { wizardAdoptionPreview, wizardAdoptionPreviewStatus } from '#lib/api/index.ts';
import type { WizardPreviewOut } from '#lib/api/types.gen.ts';

const POLL_MS = 2000;
const MAX_POLLS = 1800; // an hour: past it the screen gives up and the admin asks again

export async function previewAdoption(
  body: { destination_id: string; download_dir?: string | null },
  headers: Record<string, string>,
  pause: (ms: number) => Promise<void> = (ms) => new Promise((done) => setTimeout(done, ms))
): Promise<WizardPreviewOut> {
  const { data } = await wizardAdoptionPreview({ headers, body, throwOnError: true });
  if (!('job_id' in data)) return data;
  for (let attempt = 0; attempt < MAX_POLLS; attempt += 1) {
    await pause(POLL_MS);
    const { data: state } = await wizardAdoptionPreviewStatus({
      path: { job_id: data.job_id },
      throwOnError: true
    });
    if (state.status === 'done' && state.preview) return state.preview;
  }
  throw new Error('adoption preview did not end');
}
