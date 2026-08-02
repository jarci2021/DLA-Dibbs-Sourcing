# Save this file as .github/workflows/dibbs_daily.yml in your repo.
#
# Free daily scheduling via GitHub Actions -- no per-run cost, no usage limits
# for a public repo. For a PRIVATE repo you get 2,000 free minutes/month,
# and this job takes seconds to run, so you're nowhere close to that limit.
#
# Before this works, add these as repo secrets:
#   Repo -> Settings -> Secrets and variables -> Actions -> New repository secret
#   Add: TWIDGET_WEBHOOK_URL, TWIDGET_API_KEY (if your endpoint needs one),
#        DIBBS_FSCS (comma-separated FSC codes -- your full set of 9:
#          "5310,5305,5307,5306,5340,5330,5970,6240,5998"),
#        DIBBS_NSNS (optional, comma-separated specific NSNs)

name: DIBBS to Trello

on:
  schedule:
    - cron: "0 13 * * *"   # 13:00 UTC = 9:00 AM Eastern (adjust as needed)
  workflow_dispatch: {}     # lets you also trigger it manually from GitHub

jobs:
  run-scraper:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install requests beautifulsoup4

      - name: Run DIBBS -> Twidget script
        env:
          TWIDGET_WEBHOOK_URL: ${{ secrets.TWIDGET_WEBHOOK_URL }}
          TWIDGET_API_KEY: ${{ secrets.TWIDGET_API_KEY }}
          DIBBS_FSCS: ${{ secrets.DIBBS_FSCS }}
          DIBBS_NSNS: ${{ secrets.DIBBS_NSNS }}
          DEBUG_HTML: "true"   # saves raw HTML snapshots for debugging; turn off once things work
        run: python dibbs_to_trello.py

      # Uploads the raw HTML snapshots (see save_debug() in the script) as a
      # downloadable zip on the run's summary page -- this is how we can see
      # exactly what DIBBS actually returned at each step, without needing
      # more screenshots. Runs even if the script step above found 0 results.
      - name: Upload debug HTML snapshots
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: debug-html-snapshots
          path: debug_html/
          if-no-files-found: ignore

      # Commit the updated "seen" state file back to the repo so the next
      # scheduled run knows what's already been posted.
      - name: Commit updated state
        run: |
          git config user.name "github-actions"
          git config user.email "actions@github.com"
          git add seen_solicitations.json
          git diff --staged --quiet || git commit -m "Update seen solicitations"
          git push
