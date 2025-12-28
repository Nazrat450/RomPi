# jackett.py
from __future__ import annotations

import requests
import time
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
    timeout: int = 15,  # Reduced to 15s to fail faster and reduce Pi load
) -> requests.Response:
    base = base_url.rstrip("/")
    url = f"{base}{path}"

    p = dict(params or {})
    p["apikey"] = api_key

    # Debug logging
    print(f"DEBUG: Jackett request - URL: {base}{path}, Timeout: {timeout}s")
    print(f"DEBUG: API key being used: {api_key[:10]}...{api_key[-5:]} (length: {len(api_key)})")
    
    s = _session()
    try:
        # IMPORTANT: do NOT follow redirects. If Jackett tries to redirect to /UI/Login,
        # we want to catch it immediately.
        print(f"DEBUG: Sending GET request to {url} with API key")
        r = s.get(url, params=p, timeout=timeout, allow_redirects=False)
        print(f"DEBUG: Response received - Status: {r.status_code}, Content-Type: {r.headers.get('content-type', 'unknown')}")
        if r.is_redirect:
            print(f"DEBUG: Redirect detected - Location: {r.headers.get('location', 'N/A')}")
    except requests.exceptions.Timeout as e:
        error_msg = f"Jackett timed out after {timeout}s at {base}{path}. This usually means:\n"
        error_msg += f"  - Jackett is overloaded or slow\n"
        error_msg += f"  - Network connectivity issues\n"
        error_msg += f"  - Too many indexers being searched\n"
        error_msg += f"  - Try a more specific search query or reduce the number of indexers"
        print(f"DEBUG: Timeout error - {error_msg}")
        raise JackettError(error_msg) from e
    except requests.exceptions.ConnectionError as e:
        error_msg = f"Failed to connect to Jackett at {base_url}. Check:\n"
        error_msg += f"  - Is Jackett running?\n"
        error_msg += f"  - Is the URL correct? ({base_url})\n"
        error_msg += f"  - Can you access Jackett from this machine?"
        print(f"DEBUG: Connection error - {error_msg}")
        raise JackettError(error_msg) from e
    except requests.exceptions.RequestException as e:
        error_msg = f"Jackett request failed: {e}\n"
        error_msg += f"  URL: {base}{path}\n"
        error_msg += f"  This may indicate a network issue or Jackett configuration problem"
        print(f"DEBUG: Request exception - {error_msg}")
        raise JackettError(error_msg) from e

    # Check for login redirect, but only if we didn't get valid JSON
    ctype = (r.headers.get("content-type") or "").lower()
    is_json_response = "application/json" in ctype or (r.text and r.text.strip().startswith(("[", "{")))
    
    if _is_login_redirect(r) and not is_json_response:
        error_msg = "Jackett is redirecting API calls to login page.\n\n"
        error_msg += "Possible causes:\n"
        error_msg += "  1. Admin password is set (even if it looks blank, try clicking 'Set Password' and clearing it)\n"
        error_msg += "  2. API key is incorrect or expired\n"
        error_msg += "  3. Jackett needs to be restarted after password change\n\n"
        error_msg += "SOLUTION:\n"
        error_msg += "  1. In Jackett web UI, go to 'Jackett Configuration'\n"
        error_msg += "  2. Copy the API key shown at the top of the page\n"
        error_msg += "  3. Make sure 'Admin password' field is completely blank\n"
        error_msg += "  4. Click 'Apply server settings'\n"
        error_msg += "  5. Update the API key in config.yaml if it's different\n"
        error_msg += "  6. Restart Jackett: sudo systemctl restart jackett\n"
        error_msg += "  7. Restart Rom-Pi: sudo systemctl restart rompi\n\n"
        error_msg += f"Current API key in config: {api_key}\n"
        error_msg += f"URL attempted: {r.url}\n"
        error_msg += f"Redirected to: {r.headers.get('location', 'N/A')}\n"
        error_msg += f"Response status: {r.status_code}\n"
        error_msg += f"Content-Type: {ctype}"
        print(f"DEBUG: Login redirect detected - {error_msg}")
        raise JackettError(error_msg)

    if r.status_code >= 400:
        raise JackettError(f"Jackett HTTP {r.status_code}: {(r.text or '')[:200]}")

    return r


def _get_json(
    *,
    base_url: str,
    path: str,
    api_key: str,
    params: dict | None = None,
    timeout: int = 15,  # Reduced to 15s to fail faster and reduce Pi load
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


# DEPRECATED: This function is no longer used. Use jackett_client.search_all() instead.
def search_all_indexers(query: str, base_url: str, api_key: str, limit: int = 20, max_total_results: int = 300, delay_between_searches: float = 0.5, cancellation_check=None):
    """
    Search all indexers sequentially to prevent overwhelming Jackett and Pi.
    Searches indexers one at a time with delays between searches.
    
    Args:
        query: Search query string
        base_url: Jackett base URL
        api_key: Jackett API key
        limit: Maximum results per indexer (default 20)
        max_total_results: Stop searching when we reach this many total results (default 300)
        delay_between_searches: Seconds to wait between indexer searches (default 0.5)
        cancellation_check: Optional callable that returns True if the search should be cancelled
    """
    print(f"DEBUG: search_all_indexers called - Query: '{query}', Base URL: {base_url}, Limit: {limit}, Max Results: {max_total_results}")
    all_results = []
    
    # Test API key first
    print(f"DEBUG: Testing API key before proceeding...")
    if not test_api_key(base_url, api_key):
        raise JackettError(
            f"API key validation failed. The API key '{api_key[:10]}...' is not working.\n\n"
            f"Please verify:\n"
            f"  1. The API key in config.yaml matches the one shown in Jackett UI\n"
            f"  2. Jackett has been restarted after any configuration changes\n"
            f"  3. Try regenerating the API key in Jackett and updating config.yaml\n"
            f"  4. Check if Jackett is accessible at {base_url}"
        )
    
    # Get list of all indexers
    print(f"DEBUG: Attempting to get list of indexers from {base_url}")
    try:
        indexers = list_indexers(base_url, api_key)
        print(f"DEBUG: Successfully retrieved {len(indexers)} indexers to search")
        if len(indexers) == 0:
            print("DEBUG: WARNING - No indexers found! Check Jackett configuration.")
            raise JackettError("No indexers found in Jackett. Please configure at least one indexer in Jackett.")
    except JackettError as e:
        # Re-raise JackettError as-is
        print(f"DEBUG: JackettError getting indexer list: {e}")
        raise
    except Exception as e:
        print(f"DEBUG: Failed to get indexer list: {type(e).__name__}: {e}")
        print(f"DEBUG: Falling back to /all/results endpoint (this may be slower and more prone to timeouts)")
        # Fallback to old method if we can't get indexer list
        try:
            print(f"DEBUG: Attempting fallback search via /all/results endpoint")
            data = _get_json(
                base_url=base_url,
                path="/api/v2.0/indexers/all/results",
                api_key=api_key,
                params={"Query": query, "Limit": limit},
                timeout=10,  # Shorter timeout for fallback
            )
            results = data.get("Results", [])[:max_total_results]
            print(f"DEBUG: Fallback search returned {len(results)} results")
            return results
        except Exception as e2:
            error_msg = f"Failed to search indexers. Both methods failed:\n"
            error_msg += f"  1. Getting indexer list failed: {e}\n"
            error_msg += f"  2. Fallback /all/results failed: {e2}\n"
            error_msg += f"  Check Jackett is running and accessible at {base_url}"
            print(f"DEBUG: Both search methods failed - {error_msg}")
            raise JackettError(error_msg) from e2
    
    if not indexers:
        print("DEBUG: ERROR - No indexers found after successful list_indexers call")
        raise JackettError("No indexers available in Jackett. Please configure indexers in Jackett settings.")
    
    print(f"DEBUG: Starting sequential search through {len(indexers)} indexers")
    successful_searches = 0
    failed_searches = 0
    
    # Search each indexer sequentially
    for idx, indexer in enumerate(indexers):
        # Check for cancellation before starting each indexer search
        if cancellation_check and cancellation_check():
            print("DEBUG: Search cancelled by client - stopping immediately")
            break
        
        indexer_id = indexer.get("Id") or indexer.get("id")
        indexer_name = indexer.get("Title") or indexer.get("title") or indexer_id
        
        # Stop if we've reached the max total results
        if len(all_results) >= max_total_results:
            print(f"DEBUG: Reached max total results ({max_total_results}), stopping search")
            break
        
        # Add delay between searches (except for the first one)
        # Break delay into chunks to allow immediate cancellation
        if idx > 0:
            chunk_size = 0.1  # Check every 0.1 seconds
            remaining_delay = delay_between_searches
            while remaining_delay > 0:
                if cancellation_check and cancellation_check():
                    print("DEBUG: Search cancelled during delay - stopping immediately")
                    return all_results
                sleep_time = min(chunk_size, remaining_delay)
                time.sleep(sleep_time)
                remaining_delay -= sleep_time
        
        # Check for cancellation again before making the actual search request
        if cancellation_check and cancellation_check():
            print("DEBUG: Search cancelled before indexer search - stopping immediately")
            break
        
        try:
            print(f"DEBUG: [{idx+1}/{len(indexers)}] Searching indexer: {indexer_name} (ID: {indexer_id})")
            start_time = time.time()
            indexer_results = search_one_indexer(
                query=query,
                base_url=base_url,
                api_key=api_key,
                indexer_id=indexer_id,
                limit=limit,
            )
            elapsed = time.time() - start_time
            print(f"DEBUG: [{idx+1}/{len(indexers)}] {indexer_name} completed in {elapsed:.2f}s")
            
            # Check for cancellation after getting results
            if cancellation_check and cancellation_check():
                print("DEBUG: Search cancelled after indexer search - stopping immediately")
                break
            
            # Add results up to the max total
            remaining_slots = max_total_results - len(all_results)
            if indexer_results:
                all_results.extend(indexer_results[:remaining_slots])
                successful_searches += 1
                print(f"DEBUG: [{idx+1}/{len(indexers)}] Got {len(indexer_results)} results from {indexer_name}, total: {len(all_results)}/{max_total_results}")
            else:
                print(f"DEBUG: [{idx+1}/{len(indexers)}] {indexer_name} returned 0 results")
            
        except JackettError as e:
            # Log Jackett-specific errors but continue with other indexers
            failed_searches += 1
            print(f"DEBUG: [{idx+1}/{len(indexers)}] JackettError searching {indexer_name}: {e}")
            continue
        except Exception as e:
            # Log other errors but continue with other indexers
            failed_searches += 1
            print(f"DEBUG: [{idx+1}/{len(indexers)}] Unexpected error searching {indexer_name}: {type(e).__name__}: {e}")
            continue
    
    print(f"DEBUG: Search complete - Total results: {len(all_results)}, Successful: {successful_searches}/{len(indexers)}, Failed: {failed_searches}/{len(indexers)}")
    if failed_searches > 0:
        print(f"DEBUG: WARNING - {failed_searches} indexer(s) failed. Results may be incomplete.")
    return all_results


# DEPRECATED: Not currently used. Kept for potential future use.
def search_one_indexer(query: str, base_url: str, api_key: str, indexer_id: str, limit: int = 20):
    """
    Search a single indexer with aggressive result limiting to prevent Pi freezing.
    
    Args:
        query: Search query string
        base_url: Jackett base URL
        api_key: Jackett API key
        indexer_id: Indexer ID to search
        limit: Maximum results (default 20, reduced to prevent Pi freezing)
    """
    data = _get_json(
        base_url=base_url,
        path=f"/api/v2.0/indexers/{indexer_id}/results",
        api_key=api_key,
        params={"Query": query, "Limit": limit},
        timeout=15,  # Reduced to 15s to fail faster and reduce Pi load
    )
    return data.get("Results", [])


# DEPRECATED: No longer used. API key validation happens during actual search in jackett_client.
def test_api_key(base_url: str, api_key: str) -> bool:
    """
    Test if the API key is valid by making a simple API call.
    Returns True if API key works, False otherwise.
    DEPRECATED: Use jackett_client.search_all() instead.
    """
    try:
        print(f"DEBUG: Testing API key validity at {base_url}/api/v2.0/indexers")
        print(f"DEBUG: Using API key: {api_key[:10]}...{api_key[-5:]}")
        
        # Try direct request first to see what we get
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        s = requests.Session()
        test_url = f"{base_url.rstrip('/')}/api/v2.0/indexers"
        params = {"apikey": api_key}
        
        print(f"DEBUG: Making test request to {test_url}")
        r = s.get(test_url, params=params, timeout=10, allow_redirects=False)
        
        print(f"DEBUG: Test response - Status: {r.status_code}, Content-Type: {r.headers.get('content-type', 'N/A')}")
        print(f"DEBUG: Test response - Location header: {r.headers.get('location', 'N/A')}")
        print(f"DEBUG: Test response - First 100 chars: {(r.text or '')[:100]}")
        
        # Check if we got JSON
        if r.status_code == 200:
            ctype = (r.headers.get("content-type") or "").lower()
            if "application/json" in ctype or (r.text and r.text.strip().startswith(("[", "{"))):
                print(f"DEBUG: API key test successful - got valid JSON response")
                return True
            else:
                print(f"DEBUG: API key test - got 200 but not JSON, content-type: {ctype}")
        
        # If we got a redirect, check if it's to login
        if r.is_redirect:
            loc = (r.headers.get("location") or "").lower()
            if "/ui/login" in loc or "login" in loc:
                print(f"DEBUG: API key test - redirect to login page detected")
                return False
        
        # Try using _get_json which has better error handling
        print(f"DEBUG: Trying _get_json method...")
        data = _get_json(
            base_url=base_url,
            path="/api/v2.0/indexers",
            api_key=api_key,
            timeout=10,
        )
        print(f"DEBUG: API key test successful via _get_json - got {len(data) if isinstance(data, list) else 'data'}")
        return True
    except JackettError as e:
        print(f"DEBUG: API key test failed with JackettError: {e}")
        import traceback
        print(f"DEBUG: Full traceback:\n{traceback.format_exc()}")
        return False
    except Exception as e:
        print(f"DEBUG: API key test failed with unexpected error - {type(e).__name__}: {e}")
        import traceback
        print(f"DEBUG: Full traceback:\n{traceback.format_exc()}")
        return False


# DEPRECATED: Not currently used. Use jackett_client.get_indexers() if needed.
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
