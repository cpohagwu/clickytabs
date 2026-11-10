"""Consented cookies + localStorage -> the storage_state the agent's browser starts with.

Cookie scope comes from the user's tab's registrable domain (the extension computes it from the real tab URL, never
from the model), and is enforced again here: a cookie or origin outside the bundle's site, or a site the user did not
approve, is dropped. The result is Playwright-shaped and goes to CDP Storage.setCookies as is (browser-use applies
it), so host-only cookies use the `url` form and partitioned (CHIPS) cookies keep their partition key.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from clicky.protocol.events import SessionBundle


def in_site(host: str | None, site: str) -> bool:
    """host is the site or one of its subdomains (".shop.example" counts as shop.example)."""
    host = (host or "").strip().lstrip(".").lower()
    return bool(host) and (host == site or host.endswith("." + site))


def storage_state_from_bundle(bundle: SessionBundle, *, allowed_sites: set[str]) -> dict[str, Any]:
    if bundle.site not in allowed_sites:
        return {"cookies": [], "origins": []}
    cookies: list[dict[str, Any]] = []
    for cookie in bundle.cookies:
        host = cookie.domain.lstrip(".")
        if not in_site(host, bundle.site):
            continue
        entry: dict[str, Any] = {
            "name": cookie.name, "value": cookie.value, "path": cookie.path or "/",
            "secure": cookie.secure, "httpOnly": cookie.http_only,
        }
        if cookie.host_only:
            entry["url"] = f"{'https' if cookie.secure else 'http'}://{host}{cookie.path or '/'}"
        else:
            entry["domain"] = cookie.domain if cookie.domain.startswith(".") else f".{cookie.domain}"
        if cookie.same_site is not None:
            entry["sameSite"] = cookie.same_site
        if cookie.expires is not None and cookie.expires > 0:
            entry["expires"] = cookie.expires
        if cookie.partition_key:
            entry["partitionKey"] = {"topLevelSite": cookie.partition_key, "hasCrossSiteAncestor": False}
        cookies.append(entry)
    origins = [
        {"origin": origin.origin,
         "localStorage": [{"name": key, "value": value} for key, value in origin.local_storage.items()]}
        for origin in bundle.origins
        if urlsplit(origin.origin).scheme in ("http", "https") and in_site(urlsplit(origin.origin).hostname,
                                                                             bundle.site)
    ]
    return {"cookies": cookies, "origins": origins}


def allowed_domain_patterns(sites: list[str], urls: list[str | None]) -> list[str]:
    """browser-use navigation allowlist for the approved sites.

    Shaped for browser-use's security watchdog: "*.site" (no scheme) covers the site and its subdomains over http and
    https - used only for a site the user (or the task) is already on over plain http, i.e. local and test sites;
    otherwise "https://site/" (the apex, with the slash so "site.evil.com" can't pass its prefix match) plus
    "https://*.site" (subdomains)."""
    insecure = {site for site in sites for url in urls
                if url and urlsplit(url).scheme == "http" and in_site(urlsplit(url).hostname, site)}
    patterns: list[str] = []
    for site in sites:
        if site in insecure:
            patterns.append(f"*.{site}")
        else:
            patterns += [f"https://{site}/", f"https://*.{site}"]
    return patterns
