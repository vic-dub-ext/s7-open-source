"""s7: s7 run --paper all | s7 stage <name> --run <id> | s7 ui | s7 publish | s7 corpus"""

from __future__ import annotations

import asyncio
import threading
import time
import webbrowser

import typer

from s7.config import ConfigError, get_settings
from s7.corpus import load_papers
from s7.stages import STAGE_MODULES
from s7.store import events
from s7.store.db import get_engine
from s7.store.runs import STAGE_ORDER, create_run, list_runs

app = typer.Typer(no_args_is_help=True, add_completion=False)
corpus_app = typer.Typer(no_args_is_help=True)
app.add_typer(corpus_app, name="corpus", help="Inspect the fixed test-paper corpus.")

assert list(STAGE_MODULES) == STAGE_ORDER


@corpus_app.command("list")
def corpus_list() -> None:
    """Print the five corpus papers and what each one stress-tests."""
    papers = load_papers()
    for key, p in papers.items():
        typer.echo(f"{key}")
        typer.echo(f"  doi:   {p['doi']}")
        typer.echo(f"  title: {p['title']}")
        typer.echo(f"  tests: {', '.join(p.get('tests', []))}")
        typer.echo("")


@app.command()
def run(
    paper: str = typer.Option(
        "all", "--paper", help="paper key from corpus/papers.yaml, or 'all'"
    ),
) -> None:
    """Create a run for one or all corpus papers."""
    settings = get_settings()
    settings.ensure_dirs()
    engine = get_engine()

    papers = load_papers()
    keys = list(papers) if paper == "all" else [paper]
    unknown = [k for k in keys if k not in papers]
    if unknown:
        typer.echo(f"Unknown paper(s): {', '.join(unknown)}. Known: {', '.join(papers)}")
        raise typer.Exit(1)

    with engine.begin() as conn:
        for key in keys:
            p = papers[key]
            run_id = create_run(conn, paper_key=key, doi=p["doi"], pmcid=p.get("pmcid"))
            events.emit(
                conn,
                run_id=run_id,
                event_type="stage_started",
                message=f"run created for {key}",
                level="info",
            )
            typer.echo(f"{key}: run {run_id} created")

    typer.echo(
        "\nRun a stage with `uv run s7 stage <stage> --run <run-id>`, "
        "or inspect runs with `uv run s7 ui`."
    )


@app.command()
def stage(
    name: str,
    run_id: str = typer.Option(..., "--run", help="run id to operate on"),
    force: bool = typer.Option(False, "--force", help="re-run even if output exists"),
) -> None:
    """Run a single pipeline stage in isolation. Re-running never re-parses upstream stages."""
    if name not in STAGE_MODULES:
        typer.echo(f"Unknown stage {name!r}. Choose from: {', '.join(STAGE_MODULES)}")
        raise typer.Exit(1)

    settings = get_settings()
    settings.ensure_dirs()
    get_engine()

    try:
        result = asyncio.run(STAGE_MODULES[name].run(run_id, force=force))
    except ConfigError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except NotImplementedError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo(result.model_dump_json(indent=2))


@app.command()
def publish() -> None:
    """Run S10 for every run that has reached arbitration -- the batch
    counterpart to `s7 stage s10_publish --run <id>` for a single run.
    """
    settings = get_settings()
    settings.ensure_dirs()
    engine = get_engine()

    with engine.begin() as conn:
        all_runs = list_runs(conn)

    ready = [
        r
        for r in all_runs
        if r["stage_reached"] is not None
        and STAGE_ORDER.index(r["stage_reached"]) >= STAGE_ORDER.index("s9_arbitrate")
    ]
    if not ready:
        typer.echo("No runs have reached s9_arbitrate yet -- nothing to publish.")
        raise typer.Exit(1)

    for r in ready:
        result = asyncio.run(STAGE_MODULES["s10_publish"].run(r["id"]))
        typer.echo(f"{r['paper_key']}: {result.status} -- {result.counts}")

    typer.echo(
        f"\nWrote {settings.data_dictionary_path} and {settings.coverage_report_path}."
    )


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", help="bind address"),
    port: int = typer.Option(8420, help="bind port"),
    open_browser: bool = typer.Option(True, help="open the default browser on start"),
) -> None:
    """Start the local inspection UI at http://localhost:8420."""
    import uvicorn

    settings = get_settings()
    settings.ensure_dirs()
    get_engine()

    from s7.ui.app import app as fastapi_app

    url = f"http://{host}:{port}"
    if open_browser:

        def _open() -> None:
            time.sleep(0.75)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    typer.echo(f"s7 ui running at {url}")
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
