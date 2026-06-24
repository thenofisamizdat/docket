"""Docket — a portable ticket pipeline + autonomous dev agent you install into
any git repo. From ask to merge, in the open.

This package is a self-contained extraction of the Docket app: a standalone
FastAPI web UI (`docket_dev.app`), the autonomous agent (`docket_dev.agent`),
the SQLite store + state machine (`docket_dev.storage`), and a `docket` CLI
(`docket_dev.cli`). All per-project state lives under the target repo's
`.docket/` directory; configuration is loaded from `.docket/config.toml`.
"""

__version__ = "0.1.0"
