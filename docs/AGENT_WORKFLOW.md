# Parallel Agent Workflow

This document defines how parallel development is organised for
`swing-trading-bot`: one integration coordinator working in the root
repository, and up to three independent agents, each in its own Git worktree
on its own feature branch with its own Python environment.

Git worktrees give each agent separate working files and a separate branch.
**They are not a security sandbox.** Every worktree shares the root
repository's object store and refs, and every process on the machine can read
any path its user can read. Access restrictions are enforced by the execution
environment and by the agent's instructions, not by the worktree layout.

---

## A. Workspace ownership

| Role | Directory | Branch | Responsibility |
|---|---|---|---|
| Main Claude (integration coordinator) | repository root | `main` | Maintains local `main`; reviews and integrates approved feature branches. |
| Agent A | `agent_workspace/acquisition` | `feat/acquisition-operations` | Acquisition operations, safety, inspection and execution-readiness tooling. |
| Agent B | `agent_workspace/corporate_actions` | `feat/corporate-action-attestation` | Independent corporate-action attestation and research-readiness workflow. |
| Agent C | `agent_workspace/analytics` | `feat/backtest-analytics` | Backtest analytics, annual/quarterly performance reports and market-regime diagnostics. |

Rules:

- Each agent works only inside its assigned worktree.
- An agent must not modify files in another agent's worktree or in the root
  repository.
- `agent_workspace/` is ignored by the root repository (`/agent_workspace/`
  in `.gitignore`), so worktree contents never appear as untracked files in
  the root checkout.
- Changes to shared interfaces (modules imported by more than one area,
  configuration schemas, ledger schemas, CLI surface) must be called out
  explicitly in the agent's implementation report so the coordinator can
  check for cross-branch conflicts.

## B. Git rules

- All feature development occurs on feature branches.
- No agent directly modifies `main`.
- No agent pushes to GitHub, and no agent runs `git fetch` or `git pull`.
- No agent commits without authorization.
- No agent merges without authorization.
- No automatic rebases, resets or cherry-picks.
- Main Claude coordinates integration.
- Prefer fast-forward-only integration (`git merge --ff-only`) when possible.
- Preserve feature branches until cleanup is explicitly approved.
- The operator manually pushes approved `main` commits to GitHub.

## C. Runtime isolation

Each worktree uses:

- Its own `.venv` inside the worktree (created by `uv` from that worktree's
  `pyproject.toml` / `uv.lock`). Never copy or symlink the root `.venv`.
- Its own temporary files (prefer pytest's `tmp_path` and per-worktree temp
  directories).
- Its own test and tool caches (`.pytest_cache`, `.mypy_cache`,
  `.ruff_cache` live in the worktree).
- Synthetic research fixtures only.
- No shared mutable production or research data.

Never copy into an agent worktree: credentials or `.env` files, real
datasets, production ledgers (or their WAL/SHM sidecars), acquisition staging
or evidence, or sealed holdout data.

Offline environment setup inside a worktree:

```bash
UV_OFFLINE=1 uv sync --offline --frozen
```

If this fails because a dependency is not in the local `uv` cache, stop and
report. Do not retry online.

## D. Agent execution rules

Before beginning work, each agent must verify and record:

1. Current working directory (`pwd -P`) equals its assigned worktree.
2. Current Git branch (`git branch --show-current`) equals its assigned
   branch.
3. `HEAD` (`git rev-parse HEAD`) equals the expected starting commit given in
   its brief.
4. The working tree is clean (`git status --porcelain` is empty).
5. Its assigned file ownership (which paths it may create or modify).
6. Offline-only restrictions: no network, no credentials, no broker, no real
   data, no acquisition, no publication, no holdout access.

If the agent cannot reliably establish its assigned working directory, branch
or starting commit, it must stop and report rather than proceed.

Each agent finishes with an implementation report: changed files, tests and
static analysis run (with results), any shared-interface changes, and any
open questions.

## E. Integration workflow

Main Claude will:

1. Collect each agent's implementation report.
2. Inspect the feature branch and changed files
   (`git log main..<branch>`, `git diff main...<branch>`).
3. Verify tests and static analysis (`pytest`, `ruff check`, `ruff format
   --check`, `mypy`).
4. Check for shared-interface changes and cross-branch conflicts.
5. Request corrections where needed.
6. Obtain operator approval before committing.
7. Integrate approved work into local `main` (fast-forward only where
   possible).
8. Run integration tests on `main`.
9. Confirm the root working tree is clean.
10. Stop for operator review.

The operator manually pushes `main` to GitHub.

## F. Worktree lifecycle

**Inspect**

```bash
git worktree list
git branch --list 'feat/*'
git -C agent_workspace/<name> status --short --branch
```

**Create safely** (from the repository root, on a clean `main`):

```bash
git show-ref --verify --quiet refs/heads/<branch> && echo "branch exists - stop"
git worktree list --porcelain | grep -F "agent_workspace/<name>" && echo "registered - stop"
test -e agent_workspace/<name> && echo "path exists - stop"
git worktree add -b <branch> agent_workspace/<name> main
```

Never pass `--force` / `-f`, and never reuse an existing branch or directory
automatically. On any collision, stop and report.

**Update a worktree after approved integration** (only when the operator asks,
and only on a clean worktree):

```bash
git -C agent_workspace/<name> status --porcelain   # must be empty
git -C agent_workspace/<name> merge --ff-only main
```

If a fast-forward is not possible, stop and report; do not rebase or reset.

**Remove an unused worktree** (only with approval):

```bash
git -C agent_workspace/<name> status --porcelain   # must be empty
git worktree remove agent_workspace/<name>
```

`git worktree remove` without `--force` refuses to delete a worktree with
uncommitted or untracked changes; that refusal is the intended safeguard.
Removing a worktree does not delete its branch. Deleting a branch
(`git branch -d`) requires separate approval; `git branch -D` and
`git worktree remove --force` are not used.

**Preserve uncommitted work**: never discard changes in a worktree to make an
operation succeed. Report the dirty state to the operator instead.

## G. Acquisition authorization boundary

Creating the acquisition worktree (or any worktree) does **not** authorize:

- Live acquisition.
- Broker access.
- Credential access.
- Dataset publication.
- Research-ready attestation approval.
- Holdout evaluation.
- Trading.

All live operations remain separately gated and require explicit operator
authorization for each step.
