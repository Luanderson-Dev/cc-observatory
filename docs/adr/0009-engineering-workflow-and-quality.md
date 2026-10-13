---
status: accepted
date: 2026-10-12
decision-makers: Luanderson-Dev (maintainer)
---

# Engineering workflow and quality gates

## Context and problem statement

cc-observatory is a public repository with one maintainer and AI-assisted development.
The workflow has to keep history readable, produce versions and a changelog without
manual bookkeeping, and catch mistakes before they reach users, with the process written
in the repository rather than in GitHub settings. Which workflow and quality gates should
the project use?

## Decision drivers

- A readable history and a changelog generated from it.
- Releases cut deliberately, apart from day-to-day integration.
- The same checks locally and in CI, with one source of tool versions.
- The process lives in the repository: no rulesets or paid features, and repository
  settings change only with the maintainer's approval.
- A pinned supply chain, updated on purpose.
- AI-written changes reviewed before they are committed.

## Considered options

- **Gitflow with written process rules, Commitizen and Dependabot.** Deliberate
  releases, but release and back-merge PRs add steps, and the rules rely on discipline.
- **Trunk-based development.** Fewer branches and merges, but releases would be cut from
  the branch that receives every feature.
- **GitHub rulesets for branch protection.** Enforced by GitHub, and free for public
  repositories, but configured outside the repository and never reviewed in a PR.
- **semantic-release or python-semantic-release.** Fully automated versions and
  releases, but a release on every merge instead of when the maintainer decides.
- **Renovate instead of Dependabot.** More flexible grouping and scheduling, but an app
  to install and more configuration; Dependabot is built in and covers uv, GitHub Actions
  and pre-commit.

## Decision outcome

Chosen option: "Gitflow with written process rules, Commitizen and Dependabot", because
it gives deliberate releases and generated changelogs with tools that live in the
repository.

- **Branches.** Gitflow with git-flow-next: `develop` is the default branch and `main`
  only receives releases. Feature PRs are squash-merged with a Conventional Commit title,
  checked in CI; release and back-merge PRs use merge commits.
- **Protection.** No direct or force pushes to `main` or `develop`, and changes reach
  them only through PRs with green CI. This is a written process rule in CLAUDE.md, not
  a GitHub ruleset.
- **Versions and changelog.** Commitizen, with the uv version provider and
  `major_version_zero`.
- **Quality.** ruff, mypy in strict mode, pytest with a 90% coverage gate, and pre-commit
  running the tools through uv, so `uv.lock` is the single source of their versions.
- **CI.** GitHub Actions jobs for lint, tests on Linux, macOS and Windows, build, the PR
  title and release, with every action pinned to a full commit SHA.
- **Dependencies.** Dependabot for uv, GitHub Actions and pre-commit, targeting
  `develop`, with a 7-day cooldown.
- **Changes.** One commit per stage, and each ADR in the PR that implements it.
  Development is AI-assisted with the project subagents in `.claude/agents/`, and the
  read-only `reviewer` checks every stage before its commit.

### Consequences

- Good, because versions and the changelog come straight from commit and PR titles.
- Good, because a compromised dependency release has a week to be yanked before
  Dependabot proposes it.
- Bad, because nothing technically stops a direct push to `main` or `develop`; `ci.yml`
  also runs on pushes to both branches, so such a push is still checked and shows up.
- Bad, because Gitflow adds a release PR and a back-merge PR to every release.
- Neutral, because the process is written for one maintainer; the contributor guide
  planned for v1.0.0 explains it to others.

### Confirmation

- `.github/workflows/pr-title.yml` checks every PR title with `cz check`, and the
  commit-msg hook in `.pre-commit-config.yaml` checks local commits.
- `.github/workflows/ci.yml` runs pre-commit, the test matrix and the build;
  `fail_under = 90` in `pyproject.toml` is the coverage gate.
- `.github/dependabot.yml` lists the three ecosystems, each with `target-branch: develop`
  and a 7-day cooldown.
- The `reviewer` subagent checks every diff against this ADR, SHA-pinned actions too.

## More information

- The step-by-step feature, release and hotfix flow is in [CLAUDE.md](../../CLAUDE.md).
- Tools: [git-flow-next](https://github.com/gittower/git-flow-next) and
  [Commitizen](https://commitizen-tools.github.io/commitizen/).
