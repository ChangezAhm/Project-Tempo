"""Service configuration + Aspose.Cells license bootstrap.

The parser is a trusted backend: it talks to Supabase with the SERVICE-ROLE
key (never the publishable key), so it can write template_* rows and read
private Storage objects regardless of RLS.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_PARSER_ROOT = Path(__file__).resolve().parent.parent       # parser/
_PROJECT_ROOT = _PARSER_ROOT.parent                          # Project Tempo/

# Load parser/.env into the PROCESS environment so the Anthropic + LangSmith
# SDKs (which read os.environ: ANTHROPIC_API_KEY, LANGSMITH_*) pick up their
# keys. pydantic-settings reads the file into Settings below but does NOT
# populate os.environ. override=False keeps any real env vars authoritative.
try:
    from dotenv import load_dotenv

    load_dotenv(_PARSER_ROOT / ".env", override=False)
except Exception:  # pragma: no cover - dotenv optional
    pass


class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    storage_bucket: str = "template-files"
    parser_port: int = 8000
    # Shared secret required on data endpoints (X-API-Key header). When empty,
    # auth is DISABLED (local dev) — set it in every non-local deployment.
    parser_api_key: str = ""
    # Allowed CORS origins for the Next.js app.
    allowed_origins: str = "http://localhost:3000"
    # Aspose.Cells license — optional override (ASPOSE_CELLS_LICENSE). When
    # unset, the license is auto-discovered (see _resolve_aspose_license).
    # Without any license, rendered sheet images carry an eval watermark.
    aspose_cells_license: Path | None = None

    model_config = SettingsConfigDict(
        env_file=_PARSER_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)


settings = Settings()


# Directories searched for an Aspose.Cells license, in priority order.
_LICENSE_DIRS = [
    _PARSER_ROOT,
    _PROJECT_ROOT,
    _PROJECT_ROOT.parent,                      # ~/
    _PROJECT_ROOT.parent / "Deliverable",
]
# Filename patterns, most specific first — Aspose ships licenses under several
# names (Aspose.Cells.lic, Aspose.Cells.Product.Family.lic, Aspose.Total.lic…).
# Patterns require "Cells" before the broad fallback so we never grab a
# sibling product's license (e.g. Aspose.Slides.lic) for Cells.
_LICENSE_GLOBS = ["Aspose.Cells.lic", "Aspose*Cells*.lic", "Aspose.Total*.lic", "*.lic"]


def _resolve_aspose_license() -> Path | None:
    """Explicit ASPOSE_CELLS_LICENSE first, then auto-discover by glob."""
    explicit = settings.aspose_cells_license
    if explicit and Path(explicit).exists():
        return Path(explicit)
    for d in _LICENSE_DIRS:
        if not d.exists():
            continue
        for pattern in _LICENSE_GLOBS:
            matches = sorted(d.glob(pattern))
            if matches:
                return matches[0]
    return None


_license_applied: bool | None = None


def apply_aspose_license() -> bool:
    """Apply the Aspose.Cells license once per process (idempotent)."""
    global _license_applied
    if _license_applied is not None:
        return _license_applied

    lic_path = _resolve_aspose_license()
    if lic_path is None:
        logger.warning(
            "Aspose.Cells license not found — running in EVAL mode. Reading is "
            "unaffected, but RENDERED sheet images carry an 'Evaluation Only' "
            "watermark banner (content stays legible). Set ASPOSE_CELLS_LICENSE "
            "or drop an 'Aspose*.lic' in parser/ or the project root to remove it."
        )
        _license_applied = False
        return False
    try:
        from aspose.cells import License

        License().set_license(str(lic_path))
        logger.info("Aspose.Cells license applied from %s", lic_path.name)
        _license_applied = True
    except Exception as e:  # pragma: no cover - license edge cases
        logger.error("Failed to apply Aspose.Cells license at %s: %s", lic_path, e)
        _license_applied = False
    return _license_applied


# Apply at import so EVERY entry point (service, scripts, tests) is licensed
# before any Aspose render/save runs — not just the FastAPI startup path.
apply_aspose_license()
