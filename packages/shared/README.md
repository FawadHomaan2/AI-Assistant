# Shared contracts — Phase 2

Empty by design. This will hold the JSON Schemas that are the single source of
truth for every tool's input and output, plus the plan, step and consent shapes.

`scripts/gen-types` will generate TypeScript types for `apps/desktop` and
Pydantic models for `services/juno` from the same files, so a tool's contract
cannot drift between the UI and the core. Until the core exists, the frontend
types in `apps/desktop/src/types/index.ts` are hand-written and deliberately
narrow.
