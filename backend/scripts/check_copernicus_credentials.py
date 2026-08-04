"""Prove a CDSE OAuth client works, before wiring it into the app.

Run this straight after pasting COPERNICUS_CLIENT_ID / COPERNICUS_CLIENT_SECRET
into ``backend/.env``.  It exercises the real client in
``app.services.acquisitions.copernicus`` rather than a reimplementation, so a
pass here means the same code path the API and worker use is working.

The secret is never printed, never logged, and never sent anywhere except the
CDSE token endpoint over TLS.  Only a masked fingerprint is shown, so the
output is safe to paste into a chat or an issue.

    python backend/scripts/check_copernicus_credentials.py

Costs: one token request and one catalogue search.  Catalogue queries do not
draw on the 12 TB/month product-download quota at all.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path


REPO_BACKEND = Path(__file__).resolve().parents[1]
if str(REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(REPO_BACKEND))


def _mask(value: str | None) -> str:
    """Enough to tell two credentials apart, not enough to use one."""
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return f"set, {len(value)} chars"
    return f"{value[:4]}...{value[-2:]} ({len(value)} chars)"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify CDSE OAuth client credentials.")
    parser.add_argument(
        "--skip-search",
        action="store_true",
        help="Only check that a token can be minted; do not query the catalogue.",
    )
    args = parser.parse_args()

    from app.core.config import settings

    print("Copernicus credential check")
    print("=" * 52)
    print(f"  COPERNICUS_ENABLED       {settings.COPERNICUS_ENABLED}")
    print(f"  COPERNICUS_CLIENT_ID     {_mask(settings.COPERNICUS_CLIENT_ID)}")
    print(f"  COPERNICUS_CLIENT_SECRET {_mask(settings.COPERNICUS_CLIENT_SECRET)}")
    print(f"  token URL                {settings.COPERNICUS_TOKEN_URL}")
    print(f"  catalogue URL            {settings.COPERNICUS_CATALOGUE_URL}")
    print()

    if not settings.copernicus_configured:
        print("FAIL  Credentials are not configured.")
        print()
        print("  Set these in backend/.env, then re-run:")
        print("    COPERNICUS_ENABLED=true")
        print("    COPERNICUS_CLIENT_ID=<id from the CDSE dashboard>")
        print("    COPERNICUS_CLIENT_SECRET=<secret shown once at creation>")
        return 1

    from app.services.acquisitions import copernicus

    # 1. Token. This is the step that actually validates the credential pair.
    try:
        token = copernicus.access_token()
    except copernicus.CopernicusAuthError as exc:
        print(f"FAIL  The provider rejected these credentials.\n      {exc}")
        print()
        print("  Most likely: the id or secret was mistyped or truncated, or the")
        print("  OAuth client was deleted. Recreate it in the CDSE dashboard under")
        print("  User settings -> OAuth clients and paste both values again.")
        return 1
    except copernicus.CopernicusError as exc:
        print(f"FAIL  Could not reach the token endpoint.\n      {exc}")
        print("      This looks like a network or provider outage, not a bad key.")
        return 1

    print(f"PASS  Access token minted ({len(token)} chars, not shown).")

    if args.skip_search:
        print()
        print("Search skipped. The credentials are valid.")
        return 0

    # 2. A real catalogue query, with the same hard-locked product filter the
    #    app uses, over a small box and a short window.
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    bbox = copernicus.BoundingBox(west=79.0, south=12.5, east=81.5, north=14.5)

    try:
        products = copernicus.search_products(bbox=bbox, start=start, end=end, limit=5)
    except copernicus.CopernicusError as exc:
        print(f"FAIL  The catalogue search failed.\n      {exc}")
        return 1

    print(f"PASS  Catalogue search returned {len(products)} product(s).")
    print()
    if not products:
        print("  No scenes in that box and window. That is not an error -- the")
        print("  credentials work. The app searches wherever the user draws.")
        return 0

    for product in products:
        state = "online" if product.online else "long-term archive"
        size = f"{product.size_bytes / 1_000_000_000:.2f} GB" if product.size_bytes else "size unknown"
        print(f"  - {product.name}")
        print(f"      {product.product_type} / {product.polarisation_channels} / {state} / {size}")

    print()
    print("Credentials are valid and the hard-locked filter is returning")
    print("pipeline-compatible products. Copy the same two values into the")
    print("backend/.env on the EC2 box and restart the API and workers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
