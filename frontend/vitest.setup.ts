import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

// The wizard moves from one screen to the next by itself once a step went through; the tests
// watch the move instead of leaving the page (there is no router under jsdom).
vi.mock('$app/navigation', () => ({
  goto: vi.fn(async () => undefined),
  invalidate: vi.fn(),
  invalidateAll: vi.fn(),
  beforeNavigate: vi.fn(),
  afterNavigate: vi.fn(),
  onNavigate: vi.fn()
}));
