"""Shared CLI owner-authentication boundary for the #88 work-log scripts.

Plan §10: every script injects a trusted owner ``ActorContext`` through an
authentication boundary -- scripts NEVER call ``resolve_owner_actor()`` or build
an ``ActorContext`` inline. This module is that boundary for the CLI surface:
the operator PRESENTS credentials (flags, env, or interactive prompt) and they
are verified against the configured ``AIOS_OWNER_ID`` / ``AIOS_OWNER_API_KEY``
by the exact same fail-closed comparison the HTTP boundary uses
(``aios.api.security.verify_owner_credentials``).

Misconfigured server env -> exit 2 (equivalent of HTTP 503).
Wrong credentials        -> exit 2 (equivalent of HTTP 401).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from fastapi import HTTPException

from aios.actor import ActorContext
from aios.api.security import verify_owner_credentials


def add_owner_args(parser: argparse.ArgumentParser) -> None:
    """Register the credential-presentation flags shared by all #88 scripts."""
    group = parser.add_argument_group("owner authentication")
    group.add_argument(
        "--owner-id",
        default=os.environ.get("AIOS_OWNER_CLI_ID"),
        help="presented owner id (default: $AIOS_OWNER_CLI_ID, else prompted)",
    )
    group.add_argument(
        "--owner-key",
        default=os.environ.get("AIOS_OWNER_CLI_KEY"),
        help="presented owner API key (default: $AIOS_OWNER_CLI_KEY, else prompted securely)",
    )


def authenticate_owner_cli(args: argparse.Namespace) -> ActorContext:
    """Authenticate the presented credentials; exit non-zero on failure.

    Returns the trusted owner ``ActorContext`` -- the ONLY way any #88 script
    obtains an actor.
    """
    owner_id = args.owner_id or input("Owner id: ").strip()
    owner_key = args.owner_key or getpass.getpass("Owner API key: ")
    try:
        return verify_owner_credentials(owner_id, owner_key)
    except HTTPException as error:
        print(f"owner authentication failed: {error.detail}", file=sys.stderr)
        raise SystemExit(2) from error
