import { expect, type BrowserContext, type Page } from '@playwright/test';

export type FenceTarget = Page | BrowserContext;

/** Refuse and count every request whose origin is not the instance's (ENF-PRIV-001). */
export async function refuseOutsideBaseUrl(
  target: FenceTarget,
  base: string
): Promise<string[]> {
  const origin = new URL(base || 'http://localhost').origin;
  const attempted: string[] = [];
  await (target as any).route('**/*', async (route: any) => {
    const url = route.request().url();
    if (url.startsWith('data:') || new URL(url).origin === origin) {
      await route.continue();
    } else {
      attempted.push(url);
      await route.abort();
    }
  });
  return attempted;
}

export const fence = refuseOutsideBaseUrl;
export const refuseThirdParties = refuseOutsideBaseUrl;

export async function expectNoOutsideRequests(attempted: string[]): Promise<void> {
  expect(attempted).toEqual([]);
}