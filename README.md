# RBioBench review site

Source for the RBioBench task review website and its Colab notebooks.

- **Website:** https://script.google.com/macros/s/AKfycbzXe76o0xg_jTNJ978TC6NtQGO5mKjhNk6p4l_Xr-hfvQVjmcPifB5NWqpEkm4oCx-N/exec
- **Feedback:** written to a private Google Sheet owned by the project maintainer.

## What to edit

| To change | Edit | Goes live |
|---|---|---|
| Page layout, text, styles, front-end behavior | `site/Index.html` | Auto-deploy after merge to `main` (~1 min) |
| Server logic (sign-in, feedback, task loading) | `site/Code.gs` | Auto-deploy after merge to `main` |
| Figures on the home page | Replace a PNG in `figures/` | Auto-deploy after merge to `main` |
| Task content shown on the site (prompt, inputs, reference solution) | `data/tasks.json` | No deploy needed; the site reads it from GitHub (allow a few minutes for GitHub's cache) |
| A Colab notebook | `notebooks/RBioBench_<TASK>.ipynb` | Immediately; the site opens notebooks straight from GitHub |

To make a task runnable in Colab, add its notebook to `notebooks/` and set `"online_runnable": true` for that task in `data/tasks.json`.

## How deployment works

The site is a Google Apps Script web app. On every push to `main` that touches `site/`, `figures/`, or the workflow, `.github/workflows/deploy.yml`:

1. builds `site/*Figure.html` from `figures/*.png`,
2. runs `clasp push` to upload `site/` to the Apps Script project,
3. updates the existing web-app deployment, so the URL never changes.

It authenticates with the maintainer's clasp credentials, stored as the repository secret `CLASPRC_JSON`. You can also re-run it from the **Actions** tab (**Deploy site → Run workflow**).

## Previewing changes

Apps Script has no local server. For layout changes, open `site/Index.html` in a browser to check the HTML/CSS; anything that calls `google.script.run` only works on the deployed site. Keep changes small and check the live site after the deploy finishes.

## Rollback

Revert the commit on `main`, which redeploys the previous version. In an emergency, the maintainer can also point the deployment at an older version under **Deploy → Manage deployments** in the Apps Script editor.
