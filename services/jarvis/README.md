# Jarvis core (Python) — Phase 2

Empty by design. This directory holds the Python sidecar described in
[../../docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md): the FastAPI transport,
the agent roles (router, planner, critic, executor, observer, reflector), the
governance plane (policy engine, consent broker, hash-chained audit log, path
jail, emergency stop), the tool registry, the Windows adapters, the voice
pipeline, memory and persistence.

Nothing is stubbed here, because a stub that returns plausible data is worse
than an absent module — the UI currently states that the core is missing rather
than talking to a fake one.

Planned layout is in ARCHITECTURE.md §4; the gate for Phase 2 is in
[../../docs/PHASES.md](../../docs/PHASES.md).
