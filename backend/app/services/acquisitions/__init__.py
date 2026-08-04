"""Second scene source: server-side product acquisition from a data provider.

The browser never receives provider credentials, provider URLs, or a bearer
token. FastAPI proxies catalogue search, and a worker performs the download.
"""
