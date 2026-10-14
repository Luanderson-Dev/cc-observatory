---
status: accepted
date: 2026-10-12
decision-makers: Luanderson-Dev (maintainer)
---

# MIT license

## Context and problem statement

cc-observatory is a public, open-source project. It needs a license that lets people
use it, change it and contribute to it, and that fits the licenses of what it depends on
and what it runs. Which license should it use?

## Decision drivers

- Easy to adopt for individuals and companies, with no conditions on internal use.
- Short and well understood.
- Compatible with the Python dependencies and with shipping configuration for the
  upstream stack.
- One maintainer, so no contributor license agreement to administer.

## Considered options

- **MIT.** Permissive and short, but with no explicit patent grant.
- **Apache-2.0.** Permissive with an explicit patent grant, but longer, with a NOTICE
  file convention and a duty to mark changed files.
- **AGPL-3.0.** Strong copyleft that also covers use over a network. It protects against
  closed hosted forks, but deters adoption, and the project is not meant to be hosted
  ([ADR 0001](0001-local-single-machine-scope.md)).
- **MPL-2.0.** File-level copyleft, so changes to our files stay open, but it is less
  familiar and adds obligations for little gain in a small tool.

## Decision outcome

Chosen option: "MIT", because it is the simplest permissive license and nothing in the
project calls for copyleft or a patent grant.

The copyright holder is "Luanderson Pimenta Mendes (Luanderson-Dev)". The license text is
in `LICENSE`, and `pyproject.toml` declares `license = "MIT"` and
`license-files = ["LICENSE"]`.

### Consequences

- Good, because anyone can use, change and redistribute the code, keeping only the
  notice.
- Bad, because there is no explicit patent grant, and others may ship closed
  derivatives.
- Neutral, because the compose stack runs upstream images under their own licenses: the
  OpenTelemetry Collector and Prometheus are Apache-2.0, and Grafana, Loki and Tempo are
  AGPL-3.0. cc-observatory runs them unmodified, as separate programs, and ships only
  their configuration.

### Confirmation

- `LICENSE` holds the MIT text and the copyright line, and `pyproject.toml` declares the
  SPDX expression and the license file; the CI build job builds distributions that
  include it.
- The planned dependencies use permissive licenses (MIT, BSD, Apache-2.0). The
  `reviewer` subagent checks that every new dependency has a license compatible with
  MIT; an automated check over the locked dependencies is considered with the release
  hardening of milestone 7.

## More information

- [The MIT license on choosealicense.com](https://choosealicense.com/licenses/mit/).
- Vendored JS and CSS files ([ADR 0004](0004-web-app-stack.md)) keep their own license
  notices.
