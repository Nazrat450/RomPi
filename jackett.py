# jackett.py
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class JackettError(RuntimeError):
    pass


def _session() -> requests.Session:
    s = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": "Rom-Pi/1.0"})
    return s


def _is_login_redirect(r: requests.Response) -> bool:
    # Jackett sometimes returns 302 -> /UI/Login, or 200 HTML login page.
    loc = (r.headers.get("location") or "").lower()
    if r.is_redirect and ("/ui/login" in loc or "login" in loc):
        return True

    ctype = (r.headers.get("content-type") or "").lower()
    if "text/html" in ctype:
        head = (r.text or "").lstrip()[:200].lower()
        if "<!doctype html" in head or "<html" in head:
            # often the login page
            if "ui/login" in (r.url or "").lower() or "cookieschecked" in (r.url or "").lower():
                return True
    return False


def _get(
    *,
    base_url: str,
    path: str,
    api_key: str,
    params: dict | None = None,
    timeout: int = 25,
) -> requests.Response:
    base = base_url.rstrip("/")
    url = f"{base}{path}"

    p = dict(params or {})
    p["apikey"] = api_key

    s = _session()
    try:
        # IMPORTANT: do NOT follow redirects. If Jackett tries to redirect to /UI/Login,
        # we want to catch it immediately.
        r = s.get(url, params=p, timeout=timeout, allow_redirects=False)
    except requests.exceptions.Timeout as e:
        raise JackettError(f"Jackett timed out after {timeout}s. Try again or increase timeout.") from e
    except requests.exceptions.RequestException as e:
        raise JackettError(f"Jackett request failed: {e}") from e

    if _is_login_redirect(r):
        raise JackettError(
            "Jackett is requiring UI login and redirecting API calls.\n"
            "Fix in Jackett: disable UI authentication OR enable API access without login.\n"
            f"URL hit: {r.url}\n"
            f"Status: {r.status_code}, Location: {r.headers.get('location')}"
        )

    if r.status_code >= 400:
        raise JackettError(f"Jackett HTTP {r.status_code}: {(r.text or '')[:200]}")

    return r


def _get_json(
    *,
    base_url: str,
    path: str,
    api_key: str,
    params: dict | None = None,
    timeout: int = 25,
):
    r = _get(base_url=base_url, path=path, api_key=api_key, params=params, timeout=timeout)

    ctype = (r.headers.get("content-type") or "").lower()
    if "application/json" not in ctype:
        # Still could be JSON, but be strict so you see what's wrong.
        head = (r.text or "")[:200]
        raise JackettError(f"Jackett returned non-JSON (content-type={ctype}). First 200 chars:\n{head}")

    try:
        return r.json()
    except ValueError as e:
        raise JackettError(f"Jackett returned invalid JSON (first 200 chars): {(r.text or '')[:200]}") from e


def search_all_indexers(query: str, base_url: str, api_key: str):
    data = _get_json(
        base_url=base_url,
        path="/api/v2.0/indexers/all/results",
        api_key=api_key,
        params={"Query": query},
        timeout=35,
    )
    return data.get("Results", [])


def search_one_indexer(query: str, base_url: str, api_key: str, indexer_id: str):
    data = _get_json(
        base_url=base_url,
        path=f"/api/v2.0/indexers/{indexer_id}/results",
        api_key=api_key,
        params={"Query": query},
        timeout=35,
    )
    return data.get("Results", [])


def list_indexers(base_url: str, api_key: str):
    data = _get_json(
        base_url=base_url,
        path="/api/v2.0/indexers",
        api_key=api_key,
        timeout=20,
    )

    if isinstance(data, dict):
        data = data.get("Indexers") or data.get("indexers") or data.get("Results") or []

    if not isinstance(data, list):
        return []

    norm = []
    for ix in data:
        if not isinstance(ix, dict):
            continue
        ix_id = ix.get("Id") or ix.get("id") or ix.get("IndexerId") or ix.get("indexerId")
        ix_title = ix.get("Title") or ix.get("title") or ix.get("Name") or ix.get("name") or str(ix_id)
        if ix_id:
            norm.append({"Id": str(ix_id), "Title": str(ix_title)})

    return norm


def download_torrent_bytes(base_url: str, api_key: str, result: dict) -> bytes:
    """
    If MagnetUri is missing, Jackett can still often provide a torrent via the result's Link/Guid.
    Jackett supports /api/v2.0/indexers/all/torrent?url=<encoded>
    and /api/v2.0/indexers/<id>/torrent?url=<encoded> in some builds.
    We'll use /all/torrent as the general path.
    """
    torrent_url = (
        result.get("Link")
        or result.get("link")
        or result.get("Guid")
        or result.get("guid")
        or ""
    )
    if not torrent_url:
        raise JackettError("No MagnetUri and no Link/Guid available to download torrent.")

    r = _get(
        base_url=base_url,
        path="/api/v2.0/indexers/all/torrent",
        api_key=api_key,
        params={"url": torrent_url},
        timeout=35,
    )

    ctype = (r.headers.get("content-type") or "").lower()
    # .torrent usually is application/x-bittorrent or application/octet-stream
    if "text/html" in ctype:
        raise JackettError("Jackett returned HTML when trying to download .torrent (likely login issue).")

    content = r.content or b""
    if len(content) < 50:
        raise JackettError("Downloaded torrent file looks empty/invalid.")
    return content
