import os
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

def get_supabase() -> Client:
    url = SUPABASE_URL.replace("/rest/v1/", "").replace("/rest/v1", "")
    return create_client(url, SUPABASE_KEY)
