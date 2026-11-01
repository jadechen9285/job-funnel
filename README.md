# job-funnel

Automated multi-source job search pipeline: ingest (Greenhouse, Ashby, ...) -> heuristic gate -> LLM scoring -> human review -> resume + cover letter -> apply.

Currently built: ingestion (Greenhouse, Ashby) and the Stage 1 heuristic gate. See the [architecture map](docs/architecture.md) for the full design, including planned stages.

```bash
uv sync
uv run python -m job_funnel.pipeline
```

## Tests

Offline unit tests (HTTP is mocked):

```bash
uv run pytest -q
uv run ruff check src tests
```

## Docs & tutorial

- [docs/architecture.md](docs/architecture.md): pipeline map, data model, job lifecycle, extension points
- [notebooks/01_getting_started.ipynb](notebooks/01_getting_started.ipynb): hands-on onboarding for new devs (`uv run jupyter lab`)
