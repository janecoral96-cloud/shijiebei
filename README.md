# World Cup daily reports

Static GitHub Pages site for daily World Cup betting reports.

## Structure

- `index.html`: date index page.
- `reports/YYYY-MM-DD.html`: one report per day.
- `scripts/generate_daily.py`: generates a report and rebuilds the index.
- `.github/workflows/daily-report.yml`: scheduled GitHub Actions workflow.

## GitHub Pages setup

In the repository settings, set **Pages > Build and deployment > Source** to
**GitHub Actions**. The workflow will generate the daily report, commit it, and
deploy the whole static site.

For AI report generation, add a repository secret named `DEEPSEEK_API_KEY`.
If the secret is missing or the API fails, the workflow still publishes a
fallback HTML page so the site remains accessible.
