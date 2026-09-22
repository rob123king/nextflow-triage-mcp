"""Console-script entry point.

Kept separate from `server.py` so importing the server object never starts a
transport: `from nextflow_triage.server import mcp` does not serve anything.
"""

import logging
import os
import sys

from nextflow_triage.server import mcp

LOG_LEVEL_ENV_VAR = "NEXTFLOW_TRIAGE_LOG_LEVEL"


def configure_logging() -> None:
    """Send all logging to stderr.

    Not a style preference. Under the stdio transport stdout *is* the JSON-RPC
    channel: one stray print, or a logging handler left on its default stream,
    writes a non-JSON line into the protocol and the host disconnects. Setting
    the stream explicitly makes that hard to do by accident.
    """
    logging.basicConfig(
        level=os.environ.get(LOG_LEVEL_ENV_VAR, "WARNING").upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    """Serve over stdio, which is what a local host (an IDE, a desktop app) speaks."""
    configure_logging()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
