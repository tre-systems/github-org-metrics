# GitHub Organization Metrics

A Python tool to fetch and analyze GitHub organization metrics, including developer activity, repository statistics, and [DORA metrics](https://dora.dev/) for measuring software delivery performance.

![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

## Features

### Developer Metrics
- Commit counts and code contribution (lines added/deleted)
- Pull requests opened, reviewed, and commented on
- Repository contribution breakdown

### Repository Metrics
- Activity levels and commit frequency
- Branch and contributor counts
- Primary programming language
- Creation and last update dates

### DORA Metrics
[DORA (DevOps Research and Assessment)](https://dora.dev/) metrics help measure software delivery performance:

| Metric | Description |
|--------|-------------|
| **Lead Time** | Time from first commit to merge (branch-to-merge time) |
| **Deployment Frequency** | How often code is deployed per repository |
| **Change Failure Rate** | Percentage of deployments that fail |
| **Mean Time to Recover** | Average recovery time after failures |

### Additional Features
- **Caching**: Save API responses locally for faster re-analysis
- **Configurable**: Analyze specific repos or top N by activity
- **CSV Export**: Export results for further analysis in spreadsheets

## Prerequisites

- [uv](https://docs.astral.sh/uv/) - Fast Python package installer and resolver
- Git
- [GitHub Personal Access Token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) with appropriate permissions

## Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/rgilks/github-org-metrics.git
   cd github-org-metrics
   ```

2. **Install dependencies:**

   ```bash
   uv sync
   ```

3. **Create a GitHub Personal Access Token:**

   Go to [GitHub Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens](https://github.com/settings/tokens?type=beta) and create a token with these permissions:

   **Repository permissions:**
   | Permission | Access |
   |------------|--------|
   | Actions | Read-only |
   | Contents | Read-only |
   | Deployments | Read-only |
   | Issues | Read-only |
   | Metadata | Read-only |
   | Pull requests | Read-only |

   **Organization permissions:**
   | Permission | Access |
   |------------|--------|
   | Administration | Read-only |
   | Members | Read-only |

   For more details, see [GitHub's permissions documentation](https://docs.github.com/en/rest/overview/permissions-required-for-fine-grained-personal-access-tokens).

4. **Set the token as an environment variable:**

   ```bash
   export GITHUB_TOKEN=your_token_here
   ```

   > **Tip:** Add this to your shell profile (`~/.bashrc`, `~/.zshrc`, etc.) for persistence.

## Usage

```bash
uv run github_metrics.py <organization> [options]
```

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `--months N` | Number of months to analyze | 3 |
| `--repos N` | Maximum repositories (when not targeting specific repos) | 20 |
| `--target-repos A B C` | Analyze specific repositories only | - |
| `--use-cache` | Use cached data if available | - |
| `--update-cache` | Refresh the cache with new data | - |
| `-v, --verbose` | Enable debug logging | - |

### Examples

```bash
# Analyze top 20 repos from the last 3 months
uv run github_metrics.py my-organization

# Analyze last 6 months, top 10 repos
uv run github_metrics.py my-organization --months 6 --repos 10

# Analyze specific repositories
uv run github_metrics.py my-organization --target-repos api-service web-app

# Use cached data for faster re-analysis
uv run github_metrics.py my-organization --use-cache

# Refresh cache and re-analyze
uv run github_metrics.py my-organization --update-cache

# Enable verbose output for debugging
uv run github_metrics.py my-organization -v
```

## Output

The script generates two CSV files:

### `<org>_github_developer_metrics.csv`

| Column | Description |
|--------|-------------|
| Developer | GitHub username |
| Commits | Number of commits in the period |
| Lines Added | Total lines of code added |
| Lines Deleted | Total lines of code deleted |
| PRs Opened | Pull requests created |
| PRs Reviewed | Pull requests reviewed |
| PR Comments | Comments on pull requests |
| Repositories | Top repositories contributed to |

### `<org>_github_repository_metrics.csv`

| Column | Description |
|--------|-------------|
| name | Repository name |
| Activity | Number of commits in the period |
| avg_branch_to_merge_time | Average hours from branch to merge |
| deployment_count | Number of CI/CD deployments |
| failure_rate | Percentage of failed deployments |
| avg_deployment_duration | Average deployment time (minutes) |
| language | Primary programming language |
| branch_count | Number of branches |
| contributor_count | Number of contributors |

## Caching

Data is cached to `<org>_github_data_cache.json`. This allows:

- **Faster re-runs**: Skip API calls when experimenting with analysis
- **Offline analysis**: Work with previously fetched data
- **Historical snapshots**: Keep records of your metrics over time

> **Note:** The cache stores raw API data. Use `--update-cache` to refresh with the latest data.

## Understanding DORA Metrics

This tool calculates DORA metrics based on your GitHub data:

### Lead Time for Changes
Measured as the time from the first commit on a branch to when it's merged. Lower is better—elite performers typically achieve less than 1 hour.

### Deployment Frequency
Calculated from GitHub Actions workflow runs. Tracks how often your CI/CD pipeline successfully deploys. Elite performers deploy on demand (multiple times per day).

### Change Failure Rate
The percentage of deployments that result in failures (based on workflow run conclusions). Elite performers have less than 15% failure rate.

### Mean Time to Recover
The average time to recover from a failed deployment. Elite performers recover in less than 1 hour.

For more on DORA metrics and how to improve them, see:
- [DORA Research](https://dora.dev/research/)
- [DORA Quick Check](https://dora.dev/quickcheck/)
- [Four Keys to Software Delivery Performance](https://cloud.google.com/blog/products/devops-sre/using-the-four-keys-to-measure-your-devops-performance)

## Limitations

- **Rate limits**: The GitHub API has rate limits. Use `--use-cache` to minimize API calls.
- **Large organizations**: May take a while to fetch data for organizations with many active repositories.
- **Permissions**: Some metrics require specific token permissions. Ensure your token has all required scopes.
- **DORA accuracy**: Metrics are approximated from available GitHub data. For example, deployment frequency relies on GitHub Actions workflows.

## Troubleshooting

### "Rate limit exceeded"
The script automatically waits and retries when rate limited. For faster runs, use `--use-cache` after the initial fetch.

### "Permission error"
Ensure your GitHub token has all the required permissions listed in the Installation section.

### "Repository not found"
Check that:
1. The repository exists and is accessible to your token
2. You're using the correct organization name
3. Your token has access to the organization

### Dependency issues
```bash
uv sync
```

## Development

This project uses modern Python tooling:

- **[uv](https://docs.astral.sh/uv/)**: Package management
- **[ruff](https://docs.astral.sh/ruff/)**: Linting and formatting

```bash
# Lint code
uv run ruff check .

# Format code
uv run ruff format .
```

## License

This project is open-source and available under the [MIT License](LICENSE).

## References

- [GitHub REST API Documentation](https://docs.github.com/en/rest)
- [DORA Research Program](https://dora.dev/)
- [uv Documentation](https://docs.astral.sh/uv/)
- [Creating Fine-grained Personal Access Tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token)
