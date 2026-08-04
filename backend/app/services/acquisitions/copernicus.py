"""Copernicus Data Space Ecosystem (CDSE) catalogue search and product download.

One synchronous implementation serves both the API (called through
``run_in_threadpool``, matching how ``supabase`` and ``boto3`` are used
everywhere else) and the M3 worker.

Two properties in here are load-bearing and easy to lose in a refactor:

* The catalogue filter hard-locks ``productType`` and ``polarisationChannels``
  to what the processing pipeline can actually read.  These are pipeline
  requirements, not user preferences, so they are never parameters.
* The bearer token is re-attached across a redirect only when the target host
  passes an exact-or-dotted-suffix allowlist.  ``follow_redirects=True`` would
  forward ``Authorization`` cross-host with no way to intervene, so the
  redirect loop below is deliberately manual.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
import threading
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import httpx

from app.core.config import settings


logger = logging.getLogger(__name__)

# The pipeline contract, verified against the processing code:
#   * stages.py sorts measurement bands with `0 if "vv" in name else 1`
#   * scattering_map.py hardcodes ground_sampling_m = block * 10 (GRDH's 10 m)
#   * the scattering block, mechanism map, and land cover all need both pols
# A product outside this shape reaches `ready` with those blocks silently
# missing, so the catalogue query refuses to return anything else.
REQUIRED_PRODUCT_TYPE = "IW_GRDH_1S"
REQUIRED_POLARISATION_CHANNELS = "VV&VH"
# S1D exists; 1SDV is dual-pol VV+VH, while 1SSV/1SSH are single-pol.
PRODUCT_NAME_PATTERN = re.compile(r"^S1[A-D]_IW_GRDH_1SDV_")

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class CopernicusError(RuntimeError):
    """Base class for every provider failure."""


class CopernicusAuthError(CopernicusError):
    """Credentials were rejected. Permanent until an operator fixes them."""


class CopernicusUnavailableError(CopernicusError):
    """A transient provider failure that should use the durable retry path."""


class CopernicusProductNotFoundError(CopernicusError):
    """The requested product id does not exist in the provider catalogue."""


class CopernicusProductOfflineError(CopernicusError):
    """The product is in the long-term archive and must be ordered first."""


class CopernicusRedirectRejectedError(CopernicusError):
    """A download redirect pointed outside the trusted host allowlist."""


class CopernicusAbortedError(CopernicusError):
    """The caller asked to stop, typically because a worker lease was lost."""


@dataclass(frozen=True, slots=True)
class CopernicusProduct:
    """One catalogue result, reduced to what the app persists or renders."""

    product_id: str
    name: str
    product_type: str | None
    polarisation_channels: str | None
    sensing_start: datetime | None
    online: bool
    size_bytes: int | None
    footprint: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "product_type": self.product_type,
            "polarisation_channels": self.polarisation_channels,
            "sensing_start": self.sensing_start.isoformat() if self.sensing_start else None,
            "online": self.online,
            "size_bytes": self.size_bytes,
            "footprint": self.footprint,
        }


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """A validated WGS84 search rectangle.

    A bbox rather than a free polygon is the security property of the search
    endpoint: four floats formatted with ``f"{v:.6f}"`` go into the OData
    filter, so no user-supplied string ever enters the filter expression.
    """

    west: float
    south: float
    east: float
    north: float

    def as_wkt_polygon(self) -> str:
        w, s, e, n = (
            f"{self.west:.6f}",
            f"{self.south:.6f}",
            f"{self.east:.6f}",
            f"{self.north:.6f}",
        )
        return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


class _TokenCache:
    """Process-wide client-credentials token with a refresh margin.

    Never stored in Redis: a bearer token in a shared cache is a credential at
    rest, and this one is cheap to re-mint.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: str | None = None
        self._expires_at: float = 0.0

    def get(self, mint: Callable[[], tuple[str, int]]) -> str:
        with self._lock:
            now = time.monotonic()
            if self._value is not None and now < self._expires_at:
                return self._value
            token, expires_in = mint()
            margin = settings.COPERNICUS_TOKEN_REFRESH_MARGIN_SECONDS
            # Refresh early so a token cannot expire between the check and the
            # request that uses it. Never let the margin produce a past expiry.
            self._value = token
            self._expires_at = now + max(1.0, float(expires_in) - float(margin))
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._value = None
            self._expires_at = 0.0


_token_cache = _TokenCache()


def reset_token_cache() -> None:
    """Test hook; also useful after a credential rotation."""
    _token_cache.invalidate()


def _timeout(read_seconds: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.COPERNICUS_CONNECT_TIMEOUT_SECONDS,
        read=read_seconds,
        write=settings.COPERNICUS_CONNECT_TIMEOUT_SECONDS,
        pool=settings.COPERNICUS_CONNECT_TIMEOUT_SECONDS,
    )


def _http_client(read_seconds: float, *, follow_redirects: bool = False) -> httpx.Client:
    """The single place a client is constructed, so tests can inject a transport.

    ``follow_redirects`` is False everywhere on purpose: httpx forwards the
    ``Authorization`` header across hosts when it follows redirects itself and
    offers no hook to stop it, so redirects are resolved by hand below.
    """
    return httpx.Client(timeout=_timeout(read_seconds), follow_redirects=follow_redirects)


def host_is_allowed(host: str | None, allowed: Iterable[str]) -> bool:
    """Exact host or dotted-suffix match only.

    A plain ``endswith(suffix)`` would accept
    ``dataspace.copernicus.eu.attacker.com``, which is exactly the leak this
    guard exists to prevent.
    """
    if not host:
        return False
    candidate = host.strip().lower().rstrip(".")
    for entry in allowed:
        suffix = str(entry).strip().lower().rstrip(".")
        if not suffix:
            continue
        if candidate == suffix or candidate.endswith("." + suffix):
            return True
    return False


def _request_access_token(client: httpx.Client) -> tuple[str, int]:
    client_id, client_secret = settings.require_copernicus()
    try:
        response = client.post(
            settings.COPERNICUS_TOKEN_URL,
            data={
                # Never the password grant: no account password is handled here.
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        raise CopernicusUnavailableError("The Copernicus identity service is unreachable.") from exc

    if response.status_code in {400, 401, 403}:
        raise CopernicusAuthError(
            "Copernicus rejected the configured OAuth client credentials."
        )
    if response.status_code >= 500 or response.status_code == 429:
        raise CopernicusUnavailableError(
            f"The Copernicus identity service returned {response.status_code}."
        )
    if response.status_code != 200:
        raise CopernicusError(
            f"Unexpected Copernicus token response status {response.status_code}."
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise CopernicusError("The Copernicus token response was not JSON.") from exc
    token = body.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise CopernicusError("The Copernicus token response contained no access token.")
    expires_in = body.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        expires_in = 600
    return token.strip(), expires_in


def access_token(*, force_refresh: bool = False) -> str:
    """Return a cached client-credentials bearer token."""
    if force_refresh:
        _token_cache.invalidate()
    with _http_client(settings.COPERNICUS_SEARCH_TIMEOUT_SECONDS) as client:
        return _token_cache.get(lambda: _request_access_token(client))


def _attribute_map(product: dict[str, Any]) -> dict[str, Any]:
    attributes = product.get("Attributes")
    if not isinstance(attributes, list):
        return {}
    mapped: dict[str, Any] = {}
    for attribute in attributes:
        if isinstance(attribute, dict):
            name = attribute.get("Name")
            if isinstance(name, str):
                mapped[name] = attribute.get("Value")
    return mapped


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    # OData returns fractional seconds with more precision than fromisoformat
    # accepts on older interpreters; truncate to microseconds.
    match = re.match(r"^(.*\.\d{1,6})\d*(([+-]\d{2}:\d{2})|)$", text)
    if match:
        text = match.group(1) + (match.group(2) or "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _product_from_odata(product: dict[str, Any]) -> CopernicusProduct | None:
    product_id = product.get("Id")
    name = product.get("Name")
    if not isinstance(product_id, str) or not isinstance(name, str):
        return None
    attributes = _attribute_map(product)
    size = product.get("ContentLength")
    if isinstance(size, str) and size.isdigit():
        size = int(size)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        size = None
    content_date = product.get("ContentDate")
    sensing_start = _parse_timestamp(
        content_date.get("Start") if isinstance(content_date, dict) else None
    )
    footprint = product.get("GeoFootprint")
    return CopernicusProduct(
        product_id=product_id,
        name=name,
        product_type=(
            str(attributes["productType"]) if attributes.get("productType") is not None else None
        ),
        polarisation_channels=(
            str(attributes["polarisationChannels"])
            if attributes.get("polarisationChannels") is not None
            else None
        ),
        sensing_start=sensing_start,
        # Absent means online: only archived products carry Online=false.
        online=product.get("Online") is not False,
        size_bytes=size,
        footprint=footprint if isinstance(footprint, dict) else None,
    )


def build_search_filter(bbox: BoundingBox, start: datetime, end: datetime) -> str:
    """Build the hard-locked OData filter.

    Note there is deliberately no ``Online`` term: archived products are shown
    greyed out rather than hidden, so the user learns the scene exists.
    """
    start_text = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_text = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")
    return (
        "Collection/Name eq 'SENTINEL-1'"
        f" and OData.CSC.Intersects(area=geography'SRID=4326;{bbox.as_wkt_polygon()}')"
        f" and ContentDate/Start ge {start_text}"
        f" and ContentDate/Start le {end_text}"
        " and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType'"
        f" and att/OData.CSC.StringAttribute/Value eq '{REQUIRED_PRODUCT_TYPE}')"
        " and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'polarisationChannels'"
        f" and att/OData.CSC.StringAttribute/Value eq '{REQUIRED_POLARISATION_CHANNELS}')"
    )


def search_products(
    *,
    bbox: BoundingBox,
    start: datetime,
    end: datetime,
    limit: int,
) -> list[CopernicusProduct]:
    """Query the public CDSE OData catalogue for pipeline-compatible products."""
    url = f"{settings.COPERNICUS_CATALOGUE_URL}/Products"
    params = {
        # Passed as params, never a hand-built query string: the literal '&' in
        # 'VV&VH' must be percent-encoded as %26 or the filter silently
        # truncates at that character and every polarisation comes back.
        "$filter": build_search_filter(bbox, start, end),
        "$expand": "Attributes",
        "$orderby": "ContentDate/Start desc",
        "$top": str(int(limit)),
    }
    try:
        with _http_client(settings.COPERNICUS_SEARCH_TIMEOUT_SECONDS) as client:
            response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise CopernicusUnavailableError("The Copernicus catalogue is unreachable.") from exc

    if response.status_code == 429 or response.status_code >= 500:
        raise CopernicusUnavailableError(
            f"The Copernicus catalogue returned {response.status_code}."
        )
    if response.status_code != 200:
        raise CopernicusError(
            f"The Copernicus catalogue rejected the search with status {response.status_code}."
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise CopernicusError("The Copernicus catalogue response was not JSON.") from exc

    raw = body.get("value")
    if not isinstance(raw, list):
        raise CopernicusError("The Copernicus catalogue response contained no result list.")
    results: list[CopernicusProduct] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        parsed = _product_from_odata(entry)
        # Defence in depth: the filter already locks these, but a provider-side
        # change must never let an unsupported product reach the pipeline.
        if parsed is None or not PRODUCT_NAME_PATTERN.match(parsed.name):
            continue
        if parsed.product_type != REQUIRED_PRODUCT_TYPE:
            continue
        if parsed.polarisation_channels != REQUIRED_POLARISATION_CHANNELS:
            continue
        results.append(parsed)
    return results


def get_product(product_id: str) -> CopernicusProduct:
    """Re-fetch one product so accept-time never trusts browser-sent values."""
    url = f"{settings.COPERNICUS_CATALOGUE_URL}/Products('{_safe_product_id(product_id)}')"
    try:
        with _http_client(settings.COPERNICUS_SEARCH_TIMEOUT_SECONDS) as client:
            response = client.get(url, params={"$expand": "Attributes"})
    except httpx.HTTPError as exc:
        raise CopernicusUnavailableError("The Copernicus catalogue is unreachable.") from exc

    if response.status_code == 404:
        raise CopernicusProductNotFoundError("That Copernicus product no longer exists.")
    if response.status_code == 429 or response.status_code >= 500:
        raise CopernicusUnavailableError(
            f"The Copernicus catalogue returned {response.status_code}."
        )
    if response.status_code != 200:
        raise CopernicusError(
            f"The Copernicus catalogue rejected the lookup with status {response.status_code}."
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise CopernicusError("The Copernicus catalogue response was not JSON.") from exc

    # A single-entity read returns the product at the top level; some gateways
    # wrap it in the collection shape instead.
    payload: dict[str, Any] | None = None
    if isinstance(body, dict) and isinstance(body.get("Id"), str):
        payload = body
    elif isinstance(body, dict) and isinstance(body.get("value"), list):
        for entry in body["value"]:
            if isinstance(entry, dict) and isinstance(entry.get("Id"), str):
                payload = entry
                break
    if payload is None:
        raise CopernicusProductNotFoundError("That Copernicus product no longer exists.")
    parsed = _product_from_odata(payload)
    if parsed is None:
        raise CopernicusError("The Copernicus product record was malformed.")
    return parsed


def _safe_product_id(product_id: str) -> str:
    """Keep a caller-supplied id from ever shaping the request path.

    CDSE product ids are UUIDs. Both call sites interpolate this into an OData
    key literal, so anything that is not a UUID is rejected here rather than
    being escaped and hoped for.
    """
    candidate = str(product_id or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", candidate):
        raise CopernicusProductNotFoundError("That Copernicus product id is not valid.")
    return candidate


def _resolve_download_target(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    token: str,
) -> httpx.Response:
    """Follow redirects manually, re-attaching auth only to allowed hosts.

    Returns an *open streaming* response; the caller must close it.
    """
    allowed = settings.COPERNICUS_ALLOWED_REDIRECT_HOSTS
    current_url = url
    attach_auth = host_is_allowed(urlsplit(current_url).hostname, allowed)
    for _hop in range(settings.COPERNICUS_MAX_REDIRECTS + 1):
        request_headers = dict(headers)
        if attach_auth:
            request_headers["Authorization"] = f"Bearer {token}"
        else:
            request_headers.pop("Authorization", None)

        response = client.send(
            client.build_request("GET", current_url, headers=request_headers),
            stream=True,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response

        location = response.headers.get("location")
        response.close()
        if not location:
            raise CopernicusError("A Copernicus redirect carried no target location.")
        current_url = str(httpx.URL(current_url).join(location))
        attach_auth = host_is_allowed(urlsplit(current_url).hostname, allowed)
        if not attach_auth:
            logger.info(
                "Dropping the Copernicus bearer token for an off-domain redirect host %s",
                urlsplit(current_url).hostname,
            )
    raise CopernicusRedirectRejectedError(
        "The Copernicus download exceeded the permitted number of redirects."
    )


def download_product_to(
    product_id: str,
    destination: str,
    *,
    expected_size_bytes: int | None = None,
    progress_callback: Callable[[int, int | None], None] | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> int:
    """Stream one full product to a local path and return the byte count.

    Resumes with an HTTP Range request after a mid-stream drop. That resume
    covers drops *within* one attempt only: the worker wipes its scratch
    directory after every settled attempt, so it cannot span attempts. The
    task retry budget plus the deterministic object key cover that case.
    """
    safe_id = _safe_product_id(product_id)
    token = access_token()
    url = f"{settings.COPERNICUS_DOWNLOAD_URL}/Products('{safe_id}')/$value"
    written = 0
    resumes = 0
    refreshed_auth = False

    with _http_client(settings.COPERNICUS_READ_TIMEOUT_SECONDS, follow_redirects=False) as client:
        while True:
            if should_abort is not None and should_abort():
                raise CopernicusAbortedError("The download was aborted before completion.")

            headers: dict[str, str] = {"Accept": "*/*"}
            if written:
                headers["Range"] = f"bytes={written}-"

            response = _resolve_download_target(client, url, headers=headers, token=token)
            try:
                status = response.status_code
                if status in {401, 403}:
                    response.close()
                    if refreshed_auth:
                        raise CopernicusUnavailableError(
                            "Copernicus rejected the download authorization twice."
                        )
                    # A genuine mid-download expiry is worth exactly one retry
                    # inside this attempt; the 5-attempt task budget covers the
                    # rest without burning quota on a loop.
                    refreshed_auth = True
                    _token_cache.invalidate()
                    token = access_token(force_refresh=True)
                    continue
                if status == 404:
                    response.close()
                    raise CopernicusProductNotFoundError(
                        "That Copernicus product is no longer available for download."
                    )
                if status in {429, 503} or status >= 500:
                    response.close()
                    raise CopernicusUnavailableError(
                        f"Copernicus is throttling or unavailable (status {status})."
                    )
                if status not in {200, 206}:
                    response.close()
                    raise CopernicusError(
                        f"Unexpected Copernicus download status {status}."
                    )

                # A server that ignores Range answers 200 with the whole body.
                # Restarting from zero is the only correct response; appending
                # would silently corrupt the archive.
                mode = "ab"
                if written and status == 200:
                    logger.info("Copernicus ignored the resume range; restarting the download")
                    written = 0
                    mode = "wb"
                elif not written:
                    mode = "wb"

                total = expected_size_bytes
                if total is None:
                    content_length = response.headers.get("content-length")
                    if content_length and content_length.isdigit():
                        total = written + int(content_length)

                try:
                    with open(destination, mode) as handle:
                        for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_BYTES):
                            if not chunk:
                                continue
                            if should_abort is not None and should_abort():
                                raise CopernicusAbortedError(
                                    "The download was aborted mid-stream."
                                )
                            handle.write(chunk)
                            written += len(chunk)
                            if progress_callback is not None:
                                progress_callback(written, total)
                except (httpx.HTTPError, OSError) as exc:
                    if isinstance(exc, OSError) and not isinstance(exc, httpx.HTTPError):
                        raise CopernicusError(
                            "Unable to write the downloaded product to worker scratch."
                        ) from exc
                    if resumes >= settings.COPERNICUS_DOWNLOAD_MAX_RESUMES:
                        raise CopernicusUnavailableError(
                            "The Copernicus download kept dropping after repeated resumes."
                        ) from exc
                    resumes += 1
                    logger.info(
                        "Resuming the Copernicus download at %s bytes (resume %s)",
                        written,
                        resumes,
                    )
                    continue
            finally:
                response.close()

            if expected_size_bytes is not None and written < expected_size_bytes:
                if resumes >= settings.COPERNICUS_DOWNLOAD_MAX_RESUMES:
                    raise CopernicusUnavailableError(
                        "The Copernicus download ended before the expected size was reached."
                    )
                resumes += 1
                continue
            return written
