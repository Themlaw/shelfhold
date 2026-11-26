// What a failed screen carries to +error.svelte (EF-UX-003): the text, and the identifier of the
// incident when the server gave one.
declare global {
  namespace App {
    interface Error {
      message: string;
      requestId?: string;
    }
  }
}

export {};
