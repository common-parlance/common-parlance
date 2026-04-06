"""`proxy` command — run the local HTTP capture proxy."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from ._shared import resolve_db


def run(args: argparse.Namespace) -> None:
    from common_parlance.config import load_config
    from common_parlance.consent import check_consent_interactive
    from common_parlance.proxy import create_app
    from common_parlance.upload import UploadScheduler

    config = load_config()

    # CLI flags override config values
    port = args.port if args.port is not None else config.get("port", 11435)
    upstream = args.upstream or config.get("upstream", "http://localhost:11434")
    db_path = resolve_db(args)

    # Ensure database directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # Check consent before enabling logging
    consented = check_consent_interactive()
    log_db_path = None if args.no_log or not consented else db_path

    app = create_app(upstream=upstream, db_path=log_db_path)

    log = logging.getLogger("common_parlance")
    log.info("Starting proxy on :%d -> %s", port, upstream)
    if log_db_path:
        log.info("Database: %s", db_path)

    # Start background upload scheduler
    scheduler = UploadScheduler(
        db_path=db_path,
        proxy_url=config["proxy_url"],
        api_key=config.get("api_key", ""),
        interval_hours=config.get("upload_interval_hours", 24),
    )
    scheduler.start()

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except OSError as exc:
        if "address already in use" in str(exc).lower():
            log.error("Port %d is already in use. Try --port.", port)
        else:
            raise
    finally:
        scheduler.stop()
