import { defineConfig } from '@hey-api/openapi-ts';

// The client is generated from open-api/openapi.json and never edited by hand.
export default defineConfig({
  input: '../open-api/openapi.json',
  output: 'src/lib/api',
  plugins: ['@hey-api/client-fetch']
});
