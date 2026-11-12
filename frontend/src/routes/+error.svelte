<script lang="ts">
  // A screen that cannot be shown: the generic text, never the raw message of the framework.
  import { genericProblem } from '#lib/problems.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
</script>

<main>
  <ProblemCard problem={genericProblem()} />
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 1rem;
  }
</style>
