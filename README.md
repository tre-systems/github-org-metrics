# GitHub Organization Metrics

A command-line tool that pulls a GitHub organization's recent activity from the REST API and turns it into three things reviewers actually look at: a per-developer contribution report, a per-repository health report, and CSVs you can paste into a spreadsheet.

[![CI](https://github.com/rgilks/github-org-metrics/actions/workflows/ci.yml/badge.svg)](https://github.com/rgilks/github-org-metrics/actions/workflows/ci.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

![Dashboard screenshot](screenshot.png)

## What you get

Run `github-metrics my-org` and you get:

- **Developer activity** — commits, lines added/deleted, PRs opened, PRs reviewed, PR comments, and which repos each person touched.
- **Repository health** — commit volume, PR volume, branch-to-merge time, CI run counts, CI failure rate, CI recovery time, average CI duration, and repo metadata (created/updated, language, branch/contributor counts).
- **An "outliers" report** separating any contributor with >100k lines added (usually generated code or a bulk import) so a single vendored file doesn't skew the rest of the table.
- **Two or three CSV files** so the numbers go straight into whatever you report with.

Bots (`*[bot]`) and zero-line contributors are dropped from the main developer table automatically.

## What this is — and isn't

This tool measures **what GitHub knows**: commits, PRs, reviews, and CI workflow runs. It does **not** know about production deploys, customer impact, or incidents. That means the metrics you'll see labelled "DORA-adjacent" — `Branch→Merge (h)`, `CI Runs`, `CI Fail %`, `CI Recovery (h)` — are **proxies** for DORA's Four Keys, not the real thing.

| DORA metric              | What this tool reports instead                                                  |
| ------------------------ | ------------------------------------------------------------------------------- |
| Lead time for changes    | `Branch→Merge (h)` — hours from branch first-commit to merge                    |
| Deployment frequency     | `CI Runs` — count of the dominant CI/CD workflow runs in the window             |
| Change failure rate      | `CI Fail %` — percentage of those runs that failed                              |
| Mean time to restore     | `CI Recovery (h)` — time between a failed run and the next successful one      |

These signals are useful and trend well, but true DORA requires joining GitHub data with your incident management and production deploy systems. Use these numbers for team-level reflection, not executive-level benchmarking.

## Quickstart

```bash
# Install
uv sync

# Give it a token with read-only org access
export GITHUB_TOKEN=ghp_...

# Run
uv run github-metrics my-org
```

Three CSVs land in your current directory:

- `my-org_github_developer_metrics.csv`
- `my-org_github_repository_metrics.csv`
- `my-org_github_outliers.csv` *(only if any contributor crossed the 100k-line threshold)*

## Installation

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A GitHub [fine-grained personal access token](https://github.com/settings/tokens?type=beta) with the permissions below

### Install

```bash
git clone https://github.com/rgilks/github-org-metrics.git
cd github-org-metrics
uv sync
```

### Create a token

A fine-grained PAT scoped to the target org with these **read-only** permissions:

**Repository permissions**

| Permission     | Access    |
| -------------- | --------- |
| Actions        | Read-only |
| Contents       | Read-only |
| Deployments    | Read-only |
| Issues         | Read-only |
| Metadata       | Read-only |
| Pull requests  | Read-only |

**Organization permissions**

| Permission     | Access    |
| -------------- | --------- |
| Administration | Read-only |
| Members        | Read-only |

Then export it:

```bash
export GITHUB_TOKEN=ghp_...
# add to ~/.zshrc for persistence
```

## Usage

```
github-metrics <org> [options]
```

### Options

| Flag               | Purpose                                                                     | Default |
| ------------------ | --------------------------------------------------------------------------- | ------- |
| `--months N`       | Window of history to analyze, in months                                     | `3`     |
| `--repos N`        | Limit to the top N most recently pushed repos                               | all     |
| `--target-repos …` | Only analyze the named repositories                                         | –       |
| `--use-cache`      | Reuse a saved cache from a previous run                                     | off     |
| `--update-cache`   | Refresh the cache (fetch everything again)                                  | off     |
| `--fast`           | Skip per-PR reviews/comments (far fewer API calls)                          | off     |
| `--anonymize`      | Anonymize developer names **in the console only** (CSVs keep real names)   | off     |
| `--max-prs N`      | Cap on recent PRs hydrated with review/comment detail                       | `50`    |
| `--workers N`      | Thread-pool size for per-commit stats fetches                               | `10`    |
| `--output-dir DIR` | Directory for the cache and CSVs                                            | cwd     |
| `-v, --verbose`    | Debug logging                                                               | off     |
| `--version`        | Print version and exit                                                      | –       |

### Examples

```bash
# Last three months of everything
uv run github-metrics my-org

# Six-month window, just the interesting services
uv run github-metrics my-org --months 6 --target-repos api worker web

# Skip the expensive per-PR hydration and parallel-ish everything else
uv run github-metrics my-org --fast

# Re-run the analysis without re-fetching from GitHub
uv run github-metrics my-org --use-cache

# Anonymize the console output so you can screenshot it without doxxing anyone
uv run github-metrics my-org --anonymize

# Write everything into ./reports/ instead of cwd
uv run github-metrics my-org --output-dir reports
```

## Output

### `<org>_github_developer_metrics.csv`

| Column         | Description                                                          |
| -------------- | -------------------------------------------------------------------- |
| Developer      | GitHub login                                                         |
| Commits        | Commits in the window                                                |
| Lines Added    | Total additions across those commits                                 |
| Lines Deleted  | Total deletions across those commits                                 |
| PRs Opened     | Pull requests opened in the window                                   |
| PRs Reviewed   | Pull requests reviewed (`N/A` in `--fast` mode)                      |
| PR Comments    | Review comments authored (`N/A` in `--fast` mode)                    |
| Repositories   | Top repos the developer touched, most-active first                   |

### `<org>_github_outliers.csv`

Same columns as above, holding any developer who added more than 100,000 lines in the window. Usually one of: vendored dependencies, a bulk data import, or an auto-generated file that escaped `.gitattributes`. These rarely reflect engineering effort, so they sit in a separate file.

### `<org>_github_repository_metrics.csv`

| Column              | Description                                                                               |
| ------------------- | ----------------------------------------------------------------------------------------- |
| Repository          | Repo name                                                                                 |
| Commits             | Commits in the window                                                                     |
| PRs                 | PRs opened or updated in the window                                                       |
| `Branch→Merge (h)`  | Avg hours from a branch's first commit to PR merge (proxy for lead time)                  |
| CI Runs             | Count of the dominant CI/CD workflow's runs in the window                                 |
| CI Fail %           | Percent of those runs that ended `failure`                                                |
| `CI Recovery (h)`   | Mean hours from a failed run to the next successful run (proxy for MTTR)                  |
| `CI Duration (m)`   | Mean duration in minutes of successful CI runs                                            |
| Created / Updated   | Repo creation and last-push dates                                                         |
| Language            | GitHub-detected primary language                                                          |
| Branches            | Branch count at fetch time                                                                |
| Contributors        | Contributor count at fetch time                                                           |

## How each metric is computed

**Commits and line changes** — the `/commits` endpoint for each repo since the window start. Because GitHub's list endpoint omits additions/deletions, each commit SHA is followed up with a `/commits/{sha}` call for its stats. These follow-ups run in parallel (default 10 workers) so large repos finish in a reasonable time.

**PR counts** — PRs whose `created_at` or `updated_at` falls within the window.

**Branch→Merge time** — for each merged PR in the window: the time from the first commit reachable on the PR branch to the merge timestamp, capped at 90 days to drop long-lived branches that would dominate the mean. When the branch is longer than 100 commits this is an approximation (we take the oldest commit from the first page, not the true fork point); for day-to-day feature branches it's accurate.

**CI workflow detection** — per repo, the dominant workflow name matching `ci|test|build|deploy` (most common wins), falling back to the overall most-common workflow. All CI metrics filter to runs of that single workflow, which is what you usually want and avoids conflating release-notes or dependabot-update workflows into "deployment frequency".

**CI Recovery (h)** — sort the filtered runs by time, walk forward, and for each *first* failure record the hours until the next success. Consecutive failures don't restart the clock — they count as one incident.

**Outlier split** — any developer whose `Lines Added` crosses 100,000 is pulled out into the outliers CSV so they don't compress the axis on the rest of the table.

## Caching

Data is cached to `<org>_github_data_cache.json` (or inside `--output-dir`).

- `--use-cache` runs analysis against the cache without hitting the API.
- `--update-cache` re-fetches and overwrites.
- Useful when iterating on analysis, demoing offline, or working around rate limits.
- The cache is raw API data; it's big but perfectly diffable.
- The cache file includes an internal `_schema` version. That version tracks the shape and meaning of cached fields, not just the filename.
- If you upgrade the tool and your cache was written by an older version, the CLI will warn when the `_schema` does not match the current expected version.
- Schema mismatches are treated as a best-effort fallback for inspection only: some analysis may still run, but newer metrics can be incomplete or interpreted differently.
- When in doubt after upgrading, prefer `--update-cache` to rebuild the cache before trusting the output.

## Performance

A cold run on a medium org (~20 active repos, ~1k commits, ~500 PRs) typically finishes in a few minutes. The dominant cost is the per-commit `/commits/{sha}` call for line stats, which this tool runs in a thread pool. Rate-limit 403s are handled transparently by sleeping until the reset epoch, so you can step away from a slow run and come back to a complete cache.

If you don't need review/comment attribution, `--fast` skips the per-PR hydration entirely and is dramatically faster on large repos.

## Development

```bash
uv run pytest               # test suite, no network
uv run ruff check .         # lint
uv run ruff format .        # format
uv run mypy                 # type check
```

**Pre-commit hooks** — optional but recommended:

```bash
uv tool install pre-commit
pre-commit install
```

This runs `ruff` (check + format) and basic file-hygiene hooks on every commit. The heavier checks (`mypy`, `pytest`) run in CI.

**CI** — every push and PR triggers `.github/workflows/ci.yml`, which runs lint, format check, type check, and the full test suite on Python 3.12.

The codebase is laid out as a small package:

```
github_metrics/
  models.py    Dataclasses and date helpers
  client.py    GitHub REST client: pagination, rate limits, sessions
  fetch.py    Orchestration: fetches the payload (threaded commit stats)
  analyze.py   Turns the payload into DataFrames and DORA-adjacent signals
  cli.py       argparse, cache I/O, console display, entry point
tests/         pytest suite (models, client, analyze, CLI)
```

## Limitations

- **GitHub alone isn't enough for real DORA.** See [What this is — and isn't](#what-this-is--and-isnt).
- **Line counts follow renames poorly.** Additions/deletions come straight from GitHub and mirror the diff — renames with significant content change still show as big adds + deletes.
- **Long-lived branches approximate lead time.** See `Branch→Merge (h)` above.
- **Workflow detection is heuristic.** If your CI workflow is called `"publish-docs"` and nothing matches `ci|test|build|deploy`, the tool will still pick the most-common workflow, which may not be the one you care about. Inspect `CI Runs` and the chosen workflow; file an issue if the heuristic bites you.

## Troubleshooting

**`GITHUB_TOKEN environment variable not set`** — set it or add it to your shell rc file.

**`Permission denied for …`** — your token is missing one of the permissions in the [Create a token](#create-a-token) section. Rate-limit 403s don't produce this message; they're handled silently.

**`Rate limit exceeded`** — the tool sleeps until the reset and retries automatically. If it happens repeatedly, use `--use-cache` on subsequent runs or narrow the window with `--months` and `--target-repos`.

**`Repository not found`** — the org name is wrong, the repo is private and your token lacks access, or it wasn't pushed inside the `--months` window.

## References

- [GitHub REST API](https://docs.github.com/en/rest)
- [DORA research](https://dora.dev/research/) — the real definitions the proxies above approximate
- [uv](https://docs.astral.sh/uv/)

## License

MIT — see [LICENSE](LICENSE).
