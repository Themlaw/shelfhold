import { describe, expect, it } from 'vitest';
import { parseLocation } from './location.ts';

describe('the address of the reader', () => {
  // req: EF-LEC-005
  it('says what to open, and whether it is an extract', () => {
    expect(parseLocation('/lire/unit/01HZZZZZZZZZZZZZZZZZZZZZZA', '')).toEqual({ carrierType: 'unit', carrierId: '01HZZZZZZZZZZZZZZZZZZZZZZA', scope: null });
    expect(parseLocation('/lire/unit/U1/', '?scope=excerpt')).toEqual({ carrierType: 'unit', carrierId: 'U1', scope: 'excerpt' });
    expect(parseLocation('/lire/content/C1', '?scope=full')?.scope).toBeNull(); // only an extract is asked for by the address
  });

  // req: EF-LEC-005
  it('opens nothing for an address that is not the reader\'s', () => {
    for (const path of ['/lire', '/lire/', '/lire/unit', '/lire/unit/', '/lire/unit/a/b', '/lire/un it/U1', '/lire/unit/../x', '/autre/unit/U1', '/lire/unit/U1%2f..', '/lire/unit/<script>']) {
      expect(parseLocation(path, ''), path).toBeNull();
    }
  });
});
