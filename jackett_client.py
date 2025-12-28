# jackett_client.py
"""
Clean, simple Jackett API client for torrent searches.
Uses Jackett's /all/results endpoint for fast, reliable searches.
"""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class JackettError(RuntimeError):
    """Exception raised for Jackett API errors"""
    pass


def _create_session() -> requests.Session:
    """Create a requests session with retry logic"""
    s = requests.Session()
    
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": "Rom-Pi/1.0"})
    
    return s


def search_all(
    query: str,
    base_url: str,
    api_key: str,
    limit_per_indexer: int = 20,
    max_total: int = 300,
    timeout: int = 12
) -> list[dict]:
    """
    Search all indexers using Jackett's /all/results endpoint.
    
    Args:
        query: Search query string
        base_url: Jackett base URL (e.g., "http://192.168.1.108:9117")
        api_key: Jackett API key
        limit_per_indexer: Maximum results per indexer (default 10)
        max_total: Maximum total results to return (default 100)
        timeout: Request timeout in seconds (default 12)
    
    Returns:
        List of result dictionaries in Jackett format
    
    Raises:
        JackettError: If the API call fails
    """
    if not query or not query.strip():
        return []
    
    # Normalize base URL
    base_url = base_url.rstrip("/")
    url = f"{base_url}/api/v2.0/indexers/all/results"
    
    # Prepare parameters
    params = {
        "Query": query.strip(),
        "Limit": limit_per_indexer,
        "apikey": api_key
    }
    
    # Make the request
    session = _create_session()
    try:
        response = session.get(url, params=params, timeout=timeout, allow_redirects=False)
    except requests.exceptions.Timeout:
        raise JackettError(
            f"Jackett request timed out after {timeout} seconds. "
            f"Jackett may be overloaded or slow. Try again with a more specific search query."
        )
    except requests.exceptions.ConnectionError as e:
        raise JackettError(
            f"Failed to connect to Jackett at {base_url}. "
            f"Check that Jackett is running and accessible. Error: {e}"
        )
    except requests.exceptions.RequestException as e:
        raise JackettError(f"Jackett request failed: {e}")
    
    # Check for redirects (login page)
    if response.is_redirect:
        location = response.headers.get("location", "")
        if "/ui/login" in location.lower() or "login" in location.lower():
            raise JackettError(
                "Jackett is redirecting to login page. "
                "Make sure the Admin password field in Jackett Configuration is blank, "
                "then click 'Apply server settings' and restart Jackett."
            )
    
    # Check HTTP status
    if response.status_code == 401:
        raise JackettError(
            "Jackett returned 401 Unauthorized. Check that your API key is correct in config.yaml"
        )
    elif response.status_code == 404:
        raise JackettError(
            f"Jackett endpoint not found. Check that Jackett URL is correct: {base_url}"
        )
    elif response.status_code >= 400:
        error_text = (response.text or "")[:200]
        raise JackettError(
            f"Jackett returned HTTP {response.status_code}: {error_text}"
        )
    
    # Parse JSON response
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/json" not in content_type:
        # Check if it looks like JSON anyway
        text_start = (response.text or "").strip()[:10]
        if not (text_start.startswith("[") or text_start.startswith("{")):
            raise JackettError(
                f"Jackett returned non-JSON response (content-type: {content_type}). "
                f"First 200 chars: {(response.text or '')[:200]}"
            )
    
    try:
        data = response.json()
    except ValueError as e:
        raise JackettError(
            f"Jackett returned invalid JSON. First 200 chars: {(response.text or '')[:200]}"
        ) from e
    
    # Extract results
    if not isinstance(data, dict):
        raise JackettError(f"Jackett returned unexpected data format: {type(data)}")
    
    results = data.get("Results", [])
    if not isinstance(results, list):
        raise JackettError(f"Jackett results is not a list: {type(results)}")
    
    # Debug: Count results by indexer
    indexer_counts = {}
    for result in results:
        tracker = result.get("Tracker", "Unknown")
        indexer_counts[tracker] = indexer_counts.get(tracker, 0) + 1
    
    if indexer_counts:
        indexer_summary = ", ".join([f"{tracker}: {count}" for tracker, count in indexer_counts.items()])
        print(f"DEBUG: Jackett search results by indexer: {indexer_summary}")
    
    # Limit total results
    if len(results) > max_total:
        print(f"DEBUG: Limiting results from {len(results)} to {max_total}")
        results = results[:max_total]
    
    return results


def get_indexers(base_url: str, api_key: str, timeout: int = 10) -> list[dict]:
    """
    Get list of all configured indexers from Jackett.
    
    Args:
        base_url: Jackett base URL
        api_key: Jackett API key
        timeout: Request timeout in seconds
    
    Returns:
        List of indexer dictionaries
    
    Raises:
        JackettError: If the API call fails
    """
    base_url = base_url.rstrip("/")
    url = f"{base_url}/api/v2.0/indexers"
    
    params = {"apikey": api_key}
    
    session = _create_session()
    try:
        response = session.get(url, params=params, timeout=timeout, allow_redirects=False)
    except requests.exceptions.Timeout:
        raise JackettError(f"Jackett request timed out after {timeout} seconds")
    except requests.exceptions.ConnectionError as e:
        raise JackettError(f"Failed to connect to Jackett: {e}")
    except requests.exceptions.RequestException as e:
        raise JackettError(f"Jackett request failed: {e}")
    
    # Check for redirects
    if response.is_redirect:
        location = response.headers.get("location", "")
        if "/ui/login" in location.lower():
            raise JackettError(
                "Jackett is redirecting to login page. "
                "Make sure Admin password is blank in Jackett Configuration."
            )
    
    # Check HTTP status
    if response.status_code >= 400:
        raise JackettError(f"Jackett returned HTTP {response.status_code}")
    
    # Parse JSON
    try:
        data = response.json()
    except ValueError as e:
        raise JackettError(f"Jackett returned invalid JSON: {e}")
    
    # Extract indexers
    if isinstance(data, dict):
        indexers = data.get("Indexers") or data.get("indexers") or []
    elif isinstance(data, list):
        indexers = data
    else:
        raise JackettError(f"Unexpected data format: {type(data)}")
    
    return indexers if isinstance(indexers, list) else []

