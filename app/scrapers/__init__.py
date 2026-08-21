"""Scrapers — upgraded httpx + playwright modules."""

from app.scrapers.base import ScrapeResult, fetch, close_http_client
from app.scrapers.router import route_search, route_url, route_adaptive

__all__ = [
    "ScrapeResult",
    "fetch",
    "close_http_client",
    "route_search",
    "route_url",
    "route_adaptive",
]
