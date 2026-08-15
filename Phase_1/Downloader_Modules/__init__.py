"""
Downloader_Modules — AMTCE Unified Ingestion & Security Package
=================================================================
Exposes clean, top-level facade APIs for external media ingestion, security
quarantine validation, and scheduled account scraping.

Usage:
    from Downloader_Modules import ingest, ScanResult, run_scheduled_scraper_batch
"""

from .universal_downloader import (
    ingest,
    ingest_batch,
    ingest_image,
    ingest_pdf,
    ingest_video,
    ingest_archive,
    ingest_audio,
    IngestionError,
)

from .security_validator import (
    ScanResult,
    validate_and_quarantine,
)

from .scheduled_scraper_manager import (
    get_rotated_max_two_accounts,
    run_scheduled_scraper_batch,
)

from .state_store import (
    StateStore,
    get_state_store,
)

__all__ = [
    "ingest",
    "ingest_batch",
    "ingest_image",
    "ingest_pdf",
    "ingest_video",
    "ingest_archive",
    "ingest_audio",
    "IngestionError",
    "ScanResult",
    "validate_and_quarantine",
    "get_rotated_max_two_accounts",
    "run_scheduled_scraper_batch",
    "StateStore",
    "get_state_store",
]
