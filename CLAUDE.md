# Notes for Claude Code

This repo is the RBioBench review website (Google Apps Script) plus its Colab notebooks. Read `README.md` first for what each folder does and how changes go live.

- Run `python3 scripts/check.py` before opening a PR; CI runs the same check.
- Never run `clasp push` or `clasp deploy` locally. Deployment happens only through GitHub Actions after a change merges to `main`.
- Work on a branch and open a PR to `main`; do not push to `main` directly.
- `site/Code.gs` runs on Google's V8 Apps Script runtime: no `require`/`import`, no npm packages. Use the Apps Script services (`SpreadsheetApp`, `UrlFetchApp`, `MailApp`, ...).
- Front-end code in `site/Index.html` talks to the server only through `google.script.run.<fn>()`. A new server function must be top-level in `Code.gs`, and its name must not end in `_` (a trailing underscore makes it private).
- Do not commit `site/*Figure.html`; it is generated from `figures/*.png` by `scripts/build_figures.py`.
- Do not change the feedback sheet column layout (`COL` in `Code.gs`) without a migration; existing reviews live in those columns.
- `data/tasks.json` is ~3 MB. Edit it with a script (load JSON, change fields, dump with `ensure_ascii=False`) rather than by hand, and keep it valid JSON. The site breaks if it fails to parse.
- Notebooks run in Colab with an R kernel. Keep the `Setup` cell's archive URL and SHA-256 in sync if the practice pack changes.
