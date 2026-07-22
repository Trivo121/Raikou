"""Supabase client construction for server-side domain data access."""

from __future__ import annotations

from functools import lru_cache

from supabase import Client, create_client

from app.core.config import settings


@lru_cache
def get_supabase() -> Client:
    """Return the backend-only Supabase client.

    The service role client is used only inside FastAPI.  Route-level ownership
    checks remain mandatory because a service role intentionally bypasses RLS.
    """
    url, service_key = settings.require_supabase()
    return create_client(url, service_key)


def clear_supabase_client_cache() -> None:
    """Test hook for configuration changes made within a process."""
    get_supabase.cache_clear()
