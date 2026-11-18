import { describe, expect, it } from 'vitest';
import type { PublicationView } from './engine.ts';
import {
  DEFAULT_FIT,
  effectiveDirection,
  effectiveFit,
  settingBody,
  toSettingsView,
  type SettingsView
} from './settings.ts';

const view = (direction: PublicationView['readingDirection']): PublicationView => ({
  kind: 'pages',
  scope: 'full',
  readingDirection: direction,
  title: 'Une série',
  language: 'fr',
  seriesId: '01HZZZZZZZZZZZZZZZZZZZZZZW',
  pages: []
});

describe('the settings of the reader', () => {
  // req: EF-LEC-006
  it('reads what the server resolved: a choice, or nothing (the publication wins)', () => {
    expect(toSettingsView({ direction: 'rtl', comfort: { fit: 'width' } })).toEqual({
      direction: 'rtl',
      comfort: { fit: 'width' }
    });
    expect(toSettingsView({ direction: null, comfort: {} })).toEqual({
      direction: null,
      comfort: {}
    });
  });

  // req: EF-LEC-006
  it('reads with the direction the profile chose for this series, else the publication’s own', () => {
    const chose: SettingsView = { direction: 'rtl', comfort: {} };
    expect(effectiveDirection(view('ltr'), chose)).toBe('rtl');
    expect(effectiveDirection(view('ltr'), null)).toBe('ltr');
    expect(effectiveDirection(view('webtoon'), { direction: null, comfort: {} })).toBe('webtoon');
  });

  // req: EF-LEC-006
  it('draws with the fit of this device, else the default', () => {
    expect(effectiveFit({ direction: null, comfort: { fit: 'width' } })).toBe('width');
    expect(effectiveFit(null)).toBe(DEFAULT_FIT);
    expect(effectiveFit({ direction: null, comfort: { fit: 'sideways' } })).toBe(DEFAULT_FIT);
  });

  // req: EF-LEC-006
  it('names the device for « pour cet appareil » and no one for « partout »', () => {
    const change = { kind: 'comfort' as const, key: 'fit', value: 'width' };
    expect(settingBody(change, 'device', 'web-lecture')).toEqual({
      kind: 'comfort',
      key: 'fit',
      value: 'width',
      scope: 'device',
      device_id: 'web-lecture'
    });
    expect(settingBody(change, 'everywhere', 'web-lecture')).toEqual({
      kind: 'comfort',
      key: 'fit',
      value: 'width',
      scope: 'everywhere'
    });
    expect('device_id' in settingBody(change, 'everywhere', 'web-lecture')).toBe(false);
  });
});
