"""This module implements the command line interface for nb."""

import asyncio
import logging
import os
from enum import Enum
from typing import Optional

import typer
from dotenv import load_dotenv
from rich import console, traceback
from rich.logging import RichHandler
try:
    from verlat import latest_release
except ImportError:
    latest_release = None

from nb import __version__

load_dotenv(".env")

app = typer.Typer(add_completion=False)

con = console.Console()


def topper():
    print("nb")
    version_check()
    print("\n")


class Mode(str, Enum):
    """nb works in three modes."""

    PAST = "past"
    LIVE = "live"
    SCHEDULE = "schedule"
    API = "api"
    BOT = "bot"
    WORKER = "worker"
    DASHBOARD = "dashboard"


def verbosity_callback(value: bool):
    """Set logging level."""
    traceback.install()
    if value:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            RichHandler(
                rich_tracebacks=True,
                markup=True,
            )
        ],
        force=True,
    )
    topper()
    logging.info("Verbosity turned on! This is suitable for debugging")


def version_callback(value: bool):
    """Show current version and exit."""

    if value:
        con.print(__version__)
        raise typer.Exit()


def version_check():
    if latest_release is None:
        con.print(f"Running nb version {__version__}", style="bold green")
        return
    try:
        latver = latest_release("nb").version
        if __version__ != latver:
            con.print(
                f"nb has a newer release {latver} available!",
                style="bold yellow",
            )
        else:
            con.print(f"Running latest nb version {__version__}", style="bold green")
    except Exception:
        con.print(f"Running nb version {__version__}", style="bold green")


@app.command()
def main(
    mode: Mode = typer.Argument(
        ..., help="Choose the mode in which you want to run nb.", envvar="NB_MODE"
    ),
    verbose: Optional[bool] = typer.Option(
        None,
        "--loud",
        "-l",
        callback=verbosity_callback,
        envvar="LOUD",
        help="Increase output verbosity.",
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-v",
        callback=version_callback,
        help="Show version and exit.",
    ),
):
    """The ultimate tool to automate custom telegram message forwarding.

    For updates join telegram channel @aahniks_code

    To run web interface run `nb-web` command.
    """
    if mode == Mode.API:
        from nb.multitenant.api import main as service_main

        service_main()
    elif mode == Mode.BOT:
        from nb.multitenant.control_bot import main as service_main

        service_main()
    elif mode == Mode.WORKER:
        from nb.multitenant.worker import main as service_main

        service_main()
    elif mode == Mode.DASHBOARD:
        from nb.multitenant.dashboard import main as service_main

        service_main()
    elif mode == Mode.PAST:
        from nb.past import forward_job

        asyncio.run(forward_job())
    elif mode == Mode.SCHEDULE:
        from nb.schedule import schedule_job

        asyncio.run(schedule_job())
    else:
        from nb.live import start_sync

        asyncio.run(start_sync())


# ★ 关键：允许 python -m nb.cli live --loud 直接运行
if __name__ == "__main__":
    app()
