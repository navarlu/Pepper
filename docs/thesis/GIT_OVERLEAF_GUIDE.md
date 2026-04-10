# Overleaf Git Sync Guide

## Overview

The thesis lives in two places:

- **Local working copy:** `docs/thesis/latex/main/` — edit here, build locally with LaTeX Workshop
- **Overleaf clone:** `docs/thesis/overleaf/` — a separate git repo linked to Overleaf, used only for syncing

These are independent git repos. Pushing to Overleaf does **not** push to the main Pepper codebase (GitHub), and vice versa.

## Pushing Local Changes to Overleaf

From the **project root** (`~/Projects/FEL/Pepper`):

```bash
# 1. Sync files (skips build artifacts)
rsync -av --delete \
  --exclude='.git' \
  --exclude='*.aux' --exclude='*.log' --exclude='*.synctex.gz' \
  --exclude='*.fls' --exclude='*.fdb_latexmk' \
  --exclude='*.bbl' --exclude='*.blg' --exclude='*.out' --exclude='*.toc' \
  --exclude='*.idx' --exclude='*.ilg' --exclude='*.ind' \
  --exclude='*.loc' --exclude='*.soc' --exclude='main.pdf' \
  docs/thesis/latex/main/ docs/thesis/overleaf/

# 2. Commit and push to Overleaf
cd docs/thesis/overleaf && git add -A && git commit -m "sync from local" && git push

# 3. Go back to project root
cd ~/Projects/FEL/Pepper
```

If there are no changes, git will say "nothing to commit" — that's normal.

## Pulling Changes from Overleaf (e.g. supervisor edits)

```bash
cd docs/thesis/overleaf && git pull
```

Then copy any changes you want back into your working copy:

```bash
cd ~/Projects/FEL/Pepper
rsync -av --exclude='.git' docs/thesis/overleaf/ docs/thesis/latex/main/
```

Review the diff before building locally.

## Important Notes

- Always edit in `docs/thesis/latex/main/`, not in the `overleaf/` clone directly
- The `overleaf/` directory is in `.gitignore` — it won't be pushed to GitHub
- Overleaf credentials are stored via the git token in the remote URL
- The draft version tag (`v0.1-draft | date`) appears in the footer — bump `\draftversion` in `main.tex` when needed
