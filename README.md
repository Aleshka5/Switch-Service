# Switch-Service

Control plane for the GPU/CPU services on `master-node`: OCR, Embedder, LLM and Image Gen.

Each managed service has three states — `off`, `cpu`, `gpu`. A switch checks the free RAM and VRAM budget
first, reserves it for the duration of the start, rolls back on failure, and keeps the desired state across
reboots. The admin UI is a section of the hub admin page (`filenkov.store/admin`), served by Auth-Service
through the gateway route `/api/services/*` with `auth: admin`.

Jenkins is **not** the control plane here: a build job cannot reserve memory or roll back. Jenkins (or any CI)
is at most a thin client that POSTs a state transition. See §0 and §14 of the spec.

- **Spec:** [docs/spec-service-switch.md](docs/spec-service-switch.md) — resources, state machine, API, catalogue,
  Auth-Service integration, migration of the four services, risks, acceptance criteria.

Status: spec only, no code yet.
