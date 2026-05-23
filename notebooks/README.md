# Notebooks — an educational tour of `ifsc_data`

A four-part, runnable walkthrough of the IFSC ingestion package. Each
notebook is a chapter; read them in order, top to bottom, and you'll go
from an empty clone to a populated SQLite warehouse you can query with
pandas.

> "Run All" is safe on every notebook. The first one will hit the IFSC
> API (so make sure you have internet and that `.env` is set up). The
> later ones read from the local DB only.

## Install

From the repo root:

```bash
pip install -e ".[notebook]"
jupyter lab notebooks/
```

That installs the package itself plus `jupyterlab` and `pandas`
(used for nicer table display in the later chapters). The core package
deliberately does **not** depend on pandas — it's only here for
exploration in these notebooks.

## The four chapters

| # | Notebook | What it covers | Hits the API? |
|---|----------|----------------|:-------------:|
| 0 | [`00_setup_and_first_crawl.ipynb`](00_setup_and_first_crawl.ipynb) | Install, write `.env`, run `auth` → `init` → `pull-new` → `status`. From zero to a populated warehouse. | ✓ |
| 1 | [`01_the_data_model.ipynb`](01_the_data_model.ipynb) | The 9 tables, the hydration pattern, the entity graph, your first SQL query. | |
| 2 | [`02_the_python_api.ipynb`](02_the_python_api.ipynb) | Use the package as a library: `Settings`, `Repository`, `APIClient`, retry semantics, the location parser. | ✓ (small) |
| 3 | [`03_querying_and_exporting.ipynb`](03_querying_and_exporting.ipynb) | Three real queries with pandas, the 6 denormalized export views, reading exports back. | |

## Re-running

- Notebook **00** is idempotent — running it again refreshes credentials,
  re-applies the schema (no-op), and only fetches newly-published content
  via `pull-new`.
- Notebooks **01–03** read only; running them again is always safe.
- If you change source code under `src/ifsc_data/` and want the notebooks
  to pick it up, restart the kernel (the editable install means no
  `pip` reinstall is needed).
