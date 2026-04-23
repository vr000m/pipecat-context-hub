# Offline smoke tests — vendored fixture trees

This directory contains **offline-by-design** PR-gating smoke tests that
exercise discovery + taxonomy invariants against vendored snapshots of
`pipecat-ai/pipecat` and `pipecat-ai/pipecat-examples`.

The tests under `test_pipecat_layout.py` always run on every `pytest` call —
they require no network, no git clone, no embedding model, and no Chroma.
Target wall time ≤ 15 s.

## Why offline by default

The PR gate must stay deterministic and fast. Live-network clones would:

- rate-limit on anonymous GitHub CI runners (60/hr cap)
- flake on upstream outages
- silently change behaviour when upstream reorganises `examples/`, turning
  unrelated PRs red

Instead, fixture trees are vendored under `tests/fixtures/smoke/<repo>/` and
capture a tree-only snapshot (no `.git`, no binaries, ≤ 2 MB per repo). A
separate scheduled GitHub Action (Phase 5 of the examples-topic-layout plan)
handles the "did upstream change?" question by running the same invariant
helpers against live clones and filing a single tracking issue on failure.

## Fixture layout

```
tests/fixtures/smoke/
├── pipecat/                    # pipecat-ai/pipecat — packaged layout
│   ├── examples/<topic>/…      # topic-based subtree + one flat-file topic
│   ├── src/pipecat/__init__.py # triggers packaged-project detection
│   ├── pyproject.toml
│   └── README.md
├── pipecat-examples/           # pipecat-ai/pipecat-examples — root-level layout
│   ├── simple-chatbot/…        # examples live directly at the root
│   ├── storytelling/…
│   ├── pyproject.toml
│   └── README.md
└── FIXTURE_PINS.json           # upstream SHA + capture date per repo
```

## Regenerating the fixtures

Run the refresh script manually from a machine with network access:

```sh
uv run python tests/smoke/refresh_fixtures.py
```

The script performs a shallow clone of each repo in `FIXTURE_PINS.json` at
`main`, copies `examples/` + `pyproject.toml` + `README.md` into the vendored
tree, strips `.git/` and binaries, and rewrites `FIXTURE_PINS.json` with the
new SHA + date.

Flags:

- `--ref <branch|tag|sha>` — regenerate from a specific upstream ref
- `--dry-run` — clone and report SHAs without overwriting the vendored tree

## Refresh cadence

- **Every release**, at minimum — keeps the vendored tree within a few weeks
  of upstream so drift-job failures catch real reorgs rather than stale
  fixtures.
- **Every drift-job failure** — the scheduled drift workflow files a tracking
  issue when live invariants fail; one of the remediation steps is to refresh
  the vendored snapshot (after confirming the upstream change is intentional).

## Pin-bump triage owners

Repo maintainers own pin-bump triage. When the drift workflow opens or
updates the `upstream-drift` tracking issue, maintainers:

1. Inspect the failing invariant in the issue body.
2. Decide whether it's an intentional upstream change (→ bump fixture) or a
   regression in this project (→ fix the builder / ingester).
3. If bumping the fixture, run `refresh_fixtures.py`, commit the diff, and
   close the tracking issue.

## Design constraints (for contributors)

- `invariants.py` helpers are pure and reusable. Both Phase 4 tests and the
  Phase 5 drift script import from it — do not duplicate logic.
- Invariants parameterise over `_discover_under_examples` output, NOT over
  hard-coded topic lists. Adding or renaming an upstream topic must not break
  the assertions.
- Do **not** apply `@pytest.mark.smoke` to these tests — that marker is
  reserved for the live-network drift check. Applying it would deselect these
  tests from the default PR gate.
