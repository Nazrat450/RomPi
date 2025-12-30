from __future__ import annotations

from flask import Flask, render_template, request, redirect, url_for, flash, has_request_context, jsonify
import yaml
import re
import requests
import os
import threading
import urllib3
import json
import time
from pathlib import Path

# Disable SSL warnings for vimm.net (certificate verification disabled)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from jackett import download_torrent_bytes
from jackett_client import search_all as jackett_search_all, JackettError
from qbittorrent import Qbit
from vimm import search_vimm, VimmError

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = "Eggs?"

PER_PAGE = 50
MAX_QUEUE_SIZE = 20
QUEUE_FILE = "download_queue.json"

# --- Load config safely ---
with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)

# --- Download Queue System ---
download_queue = []
queue_lock = threading.Lock()
queue_processing = False
queue_thread = None

def load_queue():
    """Load queue from file"""
    global download_queue
    try:
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                download_queue = data.get("items", [])
                print(f"QUEUE: Loaded {len(download_queue)} items from queue file")
    except Exception as e:
        print(f"QUEUE: Error loading queue: {e}")
        download_queue = []

def save_queue():
    """Save queue to file"""
    try:
        with queue_lock:
            data = {"items": download_queue}
            with open(QUEUE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
    except Exception as e:
        print(f"QUEUE: Error saving queue: {e}")

# Load queue on startup
load_queue()


def get_qbit() -> Qbit:
    return Qbit(
        cfg["qbittorrent"]["base_url"],
        cfg["qbittorrent"]["username"],
        cfg["qbittorrent"]["password"],
    )


def is_request_active():
    """
    Check if the current Flask request is still active.
    Returns False if the request has been cancelled, disconnected, or timed out.
    
    Note: Flask doesn't provide a direct way to detect client disconnection,
    but we can check if we're still in a valid request context. If the request
    was cancelled or timed out, accessing the request object may fail.
    """
    try:
        if not has_request_context():
            return False
        # Try to access the request object - if it fails, the context is invalid
        # This is a simple check that will catch most cases where the request
        # context is no longer valid
        _ = request.method
        return True
    except (RuntimeError, AttributeError):
        # Request context is no longer valid (request was cancelled/timed out)
        return False
    except Exception:
        # If anything else goes wrong, assume it's inactive to be safe
        return False


# Aria2 helper functions
def aria2_rpc_call(method, params=None):
    """Make an RPC call to Aria2"""
    aria2_config = cfg.get("aria2", {})
    rpc_url = aria2_config.get("rpc_url", "http://localhost:6800/jsonrpc")
    rpc_secret = aria2_config.get("rpc_secret", "")
    
    if params is None:
        params = []
    
    # Add token if secret is configured
    if rpc_secret:
        params = [f"token:{rpc_secret}"] + params
    
    payload = {
        "jsonrpc": "2.0",
        "id": "rompi",
        "method": method,
        "params": params
    }
    
    try:
        response = requests.post(rpc_url, json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
        if "error" in result:
            raise RuntimeError(f"Aria2 RPC error: {result['error']}")
        return result.get("result")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to connect to Aria2: {e}")


def human_size(num) -> str:
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "?"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024.0:
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} PB"


def seeders(r) -> int:
    try:
        return int(r.get("Seeders", 0))
    except Exception:
        return 0


def looks_like_movie_tv_result(r) -> bool:
    cat = (r.get("CategoryDesc") or "").lower()
    title = (r.get("Title") or "").lower()
    bad = ["movie", "movies", "tv", "television", "webrip", "bluray", "hdtv", "x264", "x265", "hevc"]
    return any(b in cat for b in bad) or any(b in title for b in bad)


def looks_like_game_result(r) -> bool:
    cat = (r.get("CategoryDesc") or "").lower()
    title = (r.get("Title") or "").lower()

    game_signals = [
        "console", "games", "pc/games",
        "xbox", "playstation", "ps4", "ps5",
        "switch", "nintendo", "wii", "3ds", "nds",
        "psp", "vita", "rom",
        "iso", "xci", "nsp", "nsz",
    ]

    return any(s in cat for s in game_signals) or any(s in title for s in game_signals)


def looks_like_ebook_audiobook(r) -> bool:
    cat = (r.get("CategoryDesc") or "").lower()
    title = (r.get("Title") or "").lower()

    book_signals = [
        "ebook", "e-book", "books", "book",
        "audiobook", "audio book", "audiobooks",
        "kindle", "epub", "mobi", "azw", "azw3", "pdf",
        "mp3", "m4b", "audible",
    ]

    non_book_signals = [
        "game", "games", "console", "pc/games", "rom",
        "movie", "movies", "tv", "television",
    ]

    if any(s in cat for s in non_book_signals) or any(s in title for s in non_book_signals):
        return False

    return any(s in cat for s in book_signals) or any(s in title for s in book_signals)


def detect_platform(r) -> str:
    text = f"{r.get('Title', '')} {r.get('CategoryDesc', '')}".lower()

    checks = [
        ("SWITCH", [" switch ", " nsw ", " nswitch ", " xci", " nsp", " nsz", " nintendo switch"]),
        ("PS5", [" ps5 ", " playstation 5", " p5 "]),
        ("PS4", [" ps4 ", " playstation 4", " p4 "]),
        ("PS3", [" ps3 ", " playstation 3"]),
        ("PS2", [" ps2 ", " playstation 2"]),
        ("PS1", [" ps1 ", " psx", " playstation 1"]),
        ("PSP", [" psp "]),
        ("VITA", [" vita", " psv "]),
        ("XBOX SERIES", ["xbox series", " xbsx", " xsx "]),
        ("XBOX ONE", ["xbox one", " xone "]),
        ("XBOX 360", ["xbox 360", " x360 "]),
        ("WII U", ["wii u", "wiiu"]),
        ("WII", [" wii "]),
        ("3DS", [" 3ds "]),
        ("DS", [" nds", " nintendo ds", " ds "]),
        ("GAMECUBE", [" gamecube", " gcn "]),
        ("N64", [" n64 ", "nintendo 64", " z64", " v64"]),
        ("SNES", [" snes ", " sfc ", ".sfc", ".smc"]),
        ("NES", [" nes ", ".nes"]),
        ("GBA", [" gba ", ".gba"]),
        ("GBC", [" gbc ", ".gbc"]),
        ("GB", [" gb ", ".gb"]),
        ("PC", ["pc/games", " windows", " win "]),
    ]

    for platform, keys in checks:
        if any(k in text for k in keys):
            return platform

    cat = (r.get("CategoryDesc") or "").lower()
    if "pc" in cat:
        return "PC"
    if "console" in cat or "games" in cat:
        return "GAME"
    return "UNKNOWN"


def detect_filetype(r) -> str:
    title = (r.get("Title") or "").lower()
    markers = [
        "xci", "nsp", "nsz", "iso", "cso", "chd", "wbfs", "rvz", "gcz",
        "zip", "7z", "rar",
        "pkg", "bin", "cue",
        "nes", "sfc", "smc", "gba", "gb", "gbc", "z64", "v64",
        "epub", "mobi", "azw", "azw3", "pdf", "mp3", "m4b",
    ]

    found = []
    for m in markers:
        if re.search(rf"\b{re.escape(m)}\b", title):
            found.append(m.upper())

    return "/".join(found[:2]) if found else "-"


def paginate(items, page: int, per_page: int):
    total = len(items)
    if total == 0:
        return [], 0, 1, 0
    total_pages = (total + per_page - 1) // per_page
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], total, page, total_pages


def decorate_results(results):
    """
    Decorate AND normalize output fields so the template can always submit something.
    Some indexers return MagnetUri, others only return Link/Guid (torrent URL).
    """
    results = sorted(results, key=seeders, reverse=True)
    for r in results:
        r["HumanSize"] = human_size(r.get("Size", 0))
        
        # For vimm.net results, use the platform from CategoryDesc if available
        # Otherwise fall back to detect_platform
        tracker = (r.get("Tracker") or "").lower()
        if "vimm" in tracker and r.get("CategoryDesc") and r.get("CategoryDesc") != "ROM":
            # Use the platform extracted from vimm.net page
            r["Platform"] = r.get("CategoryDesc", "UNKNOWN")
        else:
            # Use the existing platform detection for torrents
            r["Platform"] = detect_platform(r)
        
        r["FileType"] = detect_filetype(r)

        magnet = (
            r.get("MagnetUri")
            or r.get("MagnetURI")
            or r.get("Magnet")
            or ""
        )
        link = (
            r.get("Link")
            or r.get("link")
            or r.get("Guid")
            or r.get("guid")
            or ""
        )

        r["MagnetUri"] = str(magnet) if magnet else ""
        r["TorrentLink"] = str(link) if link else ""
        
        # Mark vimm.net results for direct download
        tracker = (r.get("Tracker") or "").lower()
        r["IsDirectDownload"] = "vimm" in tracker

    return results


def run_search(query: str, selected_indexer: str, mode: str = "games"):
    results = []
    
    # Skip Jackett entirely if mode is "direct" (only direct downloads from vimm.net)
    if mode != "direct":
        # Use clean, simple Jackett search via /all/results endpoint
        try:
            results = jackett_search_all(
                query=query,
                base_url=cfg["jackett"]["base_url"],
                api_key=cfg["jackett"]["api_key"],
                limit_per_indexer=20,  # Limit per indexer to reduce load
                max_total=300,  # Total result limit to prevent Pi overload (increased to account for filtering)
                timeout=12  # Fail fast if Jackett is slow
            )
            
            if results:
                flash(f"Found {len(results)} result(s) from Jackett", "ok")
            else:
                flash("No results found from Jackett. Try a different search query.", "info")
                
        except JackettError as e:
            # Show clear error message
            error_msg = str(e)
            # Truncate if too long for flash message
            if len(error_msg) > 400:
                error_msg = error_msg[:400] + "... (see server logs for full message)"
            flash(error_msg, "error")
            results = []
        except Exception as e:
            # Catch any unexpected errors
            error_msg = f"Unexpected error searching Jackett: {type(e).__name__}: {e}"
            print(f"DEBUG: run_search - Unexpected error: {error_msg}")
            if len(error_msg) > 400:
                error_msg = error_msg[:400] + "... (see server logs for details)"
            flash(error_msg, "error")
            results = []
    
    # Add vimm.net results - only search when mode is "direct"
    if mode == "direct":
        try:
            vimm_results = search_vimm(query)
            if vimm_results:
                results.extend(vimm_results)  # Merge lists
                # Show success message when vimm.net results are found
                flash(f"Found {len(vimm_results)} result(s) from Vimm.net", "ok")
            else:
                # No results - this means search_vimm returned empty list without error
                # This shouldn't happen if our error handling is correct, but show a message
                flash(f"Vimm.net: No results found for '{query}' (check server logs for details)", "error")
        except VimmError as e:
            # Show detailed VimmError message (includes debug info)
            error_msg = str(e)
            # Truncate if too long for flash message, but show first 500 chars
            if len(error_msg) > 500:
                error_msg = error_msg[:500] + "... (truncated, see server logs for full message)"
            flash(f"Vimm.net error: {error_msg}", "error")
            # Also log the full error for debugging
            print(f"Vimm.net detailed error:\n{error_msg}")
        except Exception as e:
            # Show detailed error for debugging
            import traceback
            error_msg = str(e)
            error_trace = traceback.format_exc()
            # Show full error message for debugging (truncate if too long)
            if len(error_msg) > 500:
                error_msg = error_msg[:500] + "... (truncated, see server logs)"
            flash(f"Vimm.net error: {error_msg}", "error")
            # Also log the full traceback for debugging
            print(f"Vimm.net error traceback:\n{error_trace}")
    
    return results


def filter_by_mode(results, mode: str):
    if mode == "books":
        return [
            r for r in results
            if looks_like_ebook_audiobook(r) and not looks_like_movie_tv_result(r)
        ]
    
    if mode == "direct":
        # Only show direct downloads (Vimm.net results)
        return [
            r for r in results
            if r.get("IsDirectDownload", False)
        ]

    # mode == "games": show only game torrents, exclude direct downloads
    return [
        r for r in results
        if looks_like_game_result(r) 
        and not looks_like_movie_tv_result(r)
        and not r.get("IsDirectDownload", False)  # Exclude direct downloads
    ]


@app.route("/", methods=["GET", "POST"])
def index():
    # current state (defaults)
    results = []
    query = ""
    selected_indexer = "all"  # Always use "all indexers" - dropdown removed
    mode = "games"

    # page (GET param only)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        # selected_indexer always "all" - dropdown removed

        only_books = request.form.get("only_books") == "on"
        only_direct = request.form.get("only_direct") == "on"
        
        if only_direct:
            mode = "direct"
        elif only_books:
            mode = "books"
        else:
            mode = "games"

        if query:
            return redirect(url_for("index", page=1, q=query, mode=mode))

    # GET: perform the search/paginate
    query = request.args.get("q", "").strip() or query
    # selected_indexer always "all" - dropdown removed
    mode = (request.args.get("mode", mode) or "games").strip()
    if mode not in ("games", "books", "direct"):
        mode = "games"

    if query:
        results = run_search(query, selected_indexer, mode)
        results_before_filter = len(results)
        results = decorate_results(results)  # Decorate first to set IsDirectDownload
        results = filter_by_mode(results, mode)  # Then filter based on mode
        results_after_filter = len(results)
        if results_before_filter > results_after_filter:
            print(f"DEBUG: Filtered {results_before_filter} results down to {results_after_filter} for mode '{mode}'")

    page_items, total_items, page, total_pages = paginate(results, page, PER_PAGE)

    return render_template(
        "index.html",
        results=page_items,
        query=query,
        mode=mode,
        page=page,
        total_pages=total_pages,
        total_items=total_items,
        per_page=PER_PAGE,
    )


@app.route("/add", methods=["POST"])
def add():
    magnet = (request.form.get("magnet") or "").strip()
    torrent_url = (request.form.get("torrent_url") or "").strip()
    title = (request.form.get("title") or "").strip()

    q = get_qbit()
    try:
        # Login to qBittorrent
        try:
            q.login()
        except requests.exceptions.RequestException as e:
            flash(f"Failed to connect to qBittorrent at {cfg['qbittorrent']['base_url']}: {e}", "error")
            return redirect(url_for("index"))
        except RuntimeError as e:
            flash(f"qBittorrent login failed: {e}. Check your username/password in config.yaml", "error")
            return redirect(url_for("index"))

        # 1) Magnet path (best)
        if magnet.startswith("magnet:"):
            try:
                q.add_magnet(magnet)
                flash(f"Sent to qBittorrent (magnet): {title}", "ok")
                return redirect(url_for("index"))
            except requests.exceptions.RequestException as e:
                flash(f"Network error adding torrent to qBittorrent: {e}", "error")
                return redirect(url_for("index"))
            except RuntimeError as e:
                flash(f"qBittorrent rejected the torrent: {e}", "error")
                return redirect(url_for("index"))

        # 2) Torrent URL: Try direct first, then fallback if needed
        if torrent_url.startswith("http://") or torrent_url.startswith("https://"):
            # Check if this is a Jackett proxy URL (contains /dl/ or jackett_apikey)
            is_jackett_proxy = "/dl/" in torrent_url or "jackett_apikey" in torrent_url
            
            print(f"DEBUG: app.py - Torrent URL: {torrent_url[:200]}")
            print(f"DEBUG: app.py - Is Jackett proxy: {is_jackett_proxy}")
            
            # Try direct URL first - even for Jackett proxy URLs, qBittorrent might handle them
            direct_error = None
            try:
                print(f"DEBUG: app.py - Attempting to add torrent URL directly to qBittorrent")
                result = q.add_url(torrent_url)
                print(f"DEBUG: app.py - qBittorrent add_url returned: {result}")
                print(f"DEBUG: app.py - qBittorrent response type: {type(result)}, value: {result}")
                flash(f"Sent to qBittorrent (direct URL): {title}", "ok")
                return redirect(url_for("index"))
            except requests.exceptions.RequestException as e:
                print(f"DEBUG: app.py - Direct URL failed with RequestException: {type(e).__name__}: {e}")
                direct_error = e
            except RuntimeError as e:
                print(f"DEBUG: app.py - Direct URL failed with RuntimeError: {e}")
                direct_error = e
            except Exception as e:
                print(f"DEBUG: app.py - Direct URL failed with unexpected exception: {type(e).__name__}: {e}")
                direct_error = e
            
            # If direct URL failed, use fallback (download torrent file, then upload)
            if direct_error is not None:
                print(f"DEBUG: app.py - Direct URL failed, using fallback method. Error: {direct_error}")
                # Download torrent file, then upload to qBittorrent
                # For Jackett proxy URLs, download directly from the proxy URL
                try:
                    if is_jackett_proxy:
                        # Jackett proxy URLs: try multiple methods
                        print(f"DEBUG: app.py - Downloading torrent from Jackett proxy URL: {torrent_url[:150]}")
                        
                        # Method 1: Try downloading directly from proxy URL
                        torrent_bytes = None
                        try:
                            proxy_response = requests.get(torrent_url, timeout=30)
                            
                            # Check status code explicitly
                            if proxy_response.status_code == 404:
                                # Proxy URL returned 404, skip to fallback methods
                                print(f"DEBUG: app.py - Proxy URL returned 404, trying fallback methods")
                                torrent_bytes = None  # Will trigger fallback
                            else:
                                proxy_response.raise_for_status()
                                
                                # Check if we got HTML (login page) instead of torrent
                                content_type = proxy_response.headers.get('content-type', '').lower()
                                if 'text/html' in content_type:
                                    raise RuntimeError("Jackett proxy returned HTML instead of torrent (likely login issue)")
                                
                                torrent_bytes = proxy_response.content
                                if len(torrent_bytes) >= 50:
                                    print(f"DEBUG: app.py - Successfully downloaded {len(torrent_bytes)} bytes from proxy URL")
                                else:
                                    raise RuntimeError("Downloaded torrent file looks empty/invalid")
                        except requests.exceptions.RequestException as req_err:
                            # Catch all request exceptions
                            print(f"DEBUG: app.py - Request exception from proxy URL: {req_err}")
                            torrent_bytes = None  # Will trigger fallback
                        
                        # If proxy download failed (404 or other error), try fallback methods
                        if torrent_bytes is None or len(torrent_bytes) < 50:
                            # Extract indexer ID from proxy URL (e.g., "blueroms" from "/dl/blueroms/")
                            from urllib.parse import urlparse
                            parsed = urlparse(torrent_url)
                            indexer_id = None
                            if '/dl/' in parsed.path:
                                parts = parsed.path.split('/dl/')
                                if len(parts) > 1:
                                    indexer_id = parts[1].split('/')[0]
                                    print(f"DEBUG: app.py - Extracted indexer ID from proxy URL: {indexer_id}")
                            
                            # Fallback 1: Try using Jackett API endpoint with specific indexer
                            if indexer_id:
                                try:
                                    print(f"DEBUG: app.py - Trying Jackett API endpoint with indexer '{indexer_id}'")
                                    print(f"DEBUG: app.py - API URL: {cfg['jackett']['base_url']}/api/v2.0/indexers/{indexer_id}/torrent")
                                    print(f"DEBUG: app.py - API params: url={torrent_url[:100]}")
                                    from jackett import _get
                                    r = _get(
                                        base_url=cfg["jackett"]["base_url"],
                                        path=f"/api/v2.0/indexers/{indexer_id}/torrent",
                                        api_key=cfg["jackett"]["api_key"],
                                        params={"url": torrent_url},
                                        timeout=35,
                                    )
                                    print(f"DEBUG: app.py - Indexer API response status: {r.status_code}")
                                    print(f"DEBUG: app.py - Indexer API response headers: {dict(r.headers)}")
                                    ctype = (r.headers.get("content-type") or "").lower()
                                    print(f"DEBUG: app.py - Indexer API content-type: {ctype}")
                                    if "text/html" in ctype:
                                        print(f"DEBUG: app.py - Indexer API returned HTML, first 200 chars: {r.text[:200]}")
                                        raise RuntimeError("Jackett returned HTML instead of torrent")
                                    torrent_bytes = r.content
                                    print(f"DEBUG: app.py - Indexer API response size: {len(torrent_bytes)} bytes")
                                    if len(torrent_bytes) >= 50:
                                        print(f"DEBUG: app.py - Successfully downloaded {len(torrent_bytes)} bytes via indexer-specific API")
                                    else:
                                        print(f"DEBUG: app.py - Indexer API response too small: {len(torrent_bytes)} bytes")
                                        raise RuntimeError("Downloaded torrent file looks empty/invalid")
                                except Exception as indexer_error:
                                    print(f"DEBUG: app.py - Indexer-specific API failed: {type(indexer_error).__name__}: {indexer_error}")
                                    import traceback
                                    print(f"DEBUG: app.py - Indexer API traceback:\n{traceback.format_exc()}")
                                    # Fall through to try /all/torrent
                            
                            # Fallback 2: Try using Jackett API endpoint with /all/torrent
                            if torrent_bytes is None or len(torrent_bytes) < 50:
                                try:
                                    print(f"DEBUG: app.py - Trying Jackett API endpoint /all/torrent")
                                    print(f"DEBUG: app.py - API URL: {cfg['jackett']['base_url']}/api/v2.0/indexers/all/torrent")
                                    print(f"DEBUG: app.py - API params: url={torrent_url[:100]}")
                                    torrent_bytes = download_torrent_bytes(
                                        base_url=cfg["jackett"]["base_url"],
                                        api_key=cfg["jackett"]["api_key"],
                                        result={"Link": torrent_url},
                                    )
                                    print(f"DEBUG: app.py - Successfully downloaded {len(torrent_bytes)} bytes via /all/torrent API")
                                except Exception as api_error:
                                    print(f"DEBUG: app.py - /all/torrent API failed: {type(api_error).__name__}: {api_error}")
                                    import traceback
                                    print(f"DEBUG: app.py - /all/torrent API traceback:\n{traceback.format_exc()}")
                                    
                                    # Check if we have a magnet link as a last resort
                                    magnet_from_form = (request.form.get("magnet") or "").strip()
                                    if magnet_from_form.startswith("magnet:"):
                                        print(f"DEBUG: app.py - All torrent download methods failed, but magnet link is available. Trying magnet instead...")
                                        try:
                                            q.add_magnet(magnet_from_form)
                                            flash(f"Sent to qBittorrent (magnet fallback): {title}", "ok")
                                            return redirect(url_for("index"))
                                        except Exception as magnet_error:
                                            print(f"DEBUG: app.py - Magnet fallback also failed: {magnet_error}")
                                            raise RuntimeError(
                                                f"All methods failed for this torrent. The Jackett proxy URL returned 404, "
                                                f"and all API download methods failed. Magnet link also failed. "
                                                f"Try a different result or check if the indexer is working properly. "
                                                f"Error: {api_error}"
                                            ) from api_error
                                    else:
                                        raise RuntimeError(
                                            f"All methods failed for this torrent. The Jackett proxy URL returned 404, "
                                            f"and all API download methods failed. No magnet link available. "
                                            f"Try a different result or check if the indexer is working properly. "
                                            f"Error: {api_error}"
                                        ) from api_error
                            
                            # Final check
                            if torrent_bytes is None or len(torrent_bytes) < 50:
                                # Check if we have a magnet link as a last resort
                                magnet_from_form = (request.form.get("magnet") or "").strip()
                                if magnet_from_form.startswith("magnet:"):
                                    print(f"DEBUG: app.py - All torrent download methods failed, but magnet link is available. Trying magnet instead...")
                                    try:
                                        q.add_magnet(magnet_from_form)
                                        flash(f"Sent to qBittorrent (magnet fallback): {title}", "ok")
                                        return redirect(url_for("index"))
                                    except Exception as magnet_error:
                                        print(f"DEBUG: app.py - Magnet fallback also failed: {magnet_error}")
                                        raise RuntimeError(
                                            f"All methods failed for this torrent. The Jackett proxy URL and all API download methods failed. "
                                            f"Magnet link also failed. Try a different result or check if the indexer is working properly."
                                        ) from magnet_error
                                else:
                                    raise RuntimeError(
                                        f"All methods failed for this torrent. The Jackett proxy URL returned 404, "
                                        f"and all API download methods failed. No magnet link available. "
                                        f"Try a different result or check if the indexer is working properly."
                                    )
                    else:
                        # For non-proxy URLs, use Jackett API to download
                        print(f"DEBUG: app.py - Using Jackett API to download torrent")
                        torrent_bytes = download_torrent_bytes(
                            base_url=cfg["jackett"]["base_url"],
                            api_key=cfg["jackett"]["api_key"],
                            result={"Link": torrent_url},
                        )
                    
                    # Successfully downloaded via Jackett, now upload to qBittorrent
                    try:
                        if hasattr(q, "add_torrent_bytes"):
                            q.add_torrent_bytes(torrent_bytes)
                            flash(f"Sent to qBittorrent (.torrent upload): {title}", "ok")
                            return redirect(url_for("index"))

                        if hasattr(q, "add_torrent_file"):
                            # Some wrappers take a file-like or bytes; support both
                            q.add_torrent_file(torrent_bytes)
                            flash(f"Sent to qBittorrent (.torrent upload): {title}", "ok")
                            return redirect(url_for("index"))

                        raise RuntimeError(
                            "Torrent URL provided, but Qbit wrapper has no add_torrent_bytes() or add_torrent_file(). "
                            "Add one of those methods to qbittorrent.py."
                        )
                    except requests.exceptions.RequestException as e:
                        flash(f"Network error adding torrent to qBittorrent: {e}", "error")
                        return redirect(url_for("index"))
                    except RuntimeError as e:
                        flash(f"qBittorrent rejected the torrent: {e}", "error")
                        return redirect(url_for("index"))
                        
                except JackettError as jackett_error:
                    # Both direct URL and Jackett proxy failed
                    flash(
                        f"Failed to add torrent. Direct URL error: {direct_error}. "
                        f"Jackett proxy also failed: {jackett_error}. "
                        f"If Jackett shows login errors, fix in Jackett: disable UI authentication OR enable API access without login.",
                        "error"
                    )
                    return redirect(url_for("index"))

        # 3) Nothing usable
        flash(
            "No magnet and no torrent URL available from Jackett result. "
            "This usually means Jackett returned incomplete data or login HTML.",
            "error",
        )
        return redirect(url_for("index"))

    except JackettError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))
    except Exception as e:
        flash(f"Unexpected error adding to qBittorrent: {e}", "error")
        return redirect(url_for("index"))


@app.route("/download", methods=["POST"])
def download():
    """Handle direct downloads from vimm.net"""
    download_url = (request.form.get("download_url") or "").strip()
    title = (request.form.get("title") or "").strip()
    game_page_url = (request.form.get("game_page_url") or "").strip()
    
    if not download_url or not download_url.startswith(("http://", "https://")):
        flash("Invalid download URL", "error")
        return redirect(url_for("index"))
    
    # Get download directory from config
    download_dir = cfg.get("downloads", {}).get("directory", "/opt/rompi/downloads")
    
    # Create directory if it doesn't exist
    os.makedirs(download_dir, exist_ok=True)
    
    # Sanitize filename from title
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.')).rstrip()
    if not safe_title:
        safe_title = "download"
    
    # Check if this is a vimm.net download (needed for file type detection)
    is_vimm_download = "vimm.net" in download_url.lower() or "dl" in download_url.lower() and "vimm.net" in download_url.lower()
    
    # Try to get filename from URL or use title
    filename = None
    detected_ext = None
    actual_file_type = None
    
    try:
        # Make HEAD request to get filename from Content-Disposition header
        # Use a shorter timeout and catch exceptions gracefully
        head_response = requests.head(download_url, allow_redirects=True, timeout=5, stream=False)
        if head_response.status_code == 200:
            content_disp = head_response.headers.get("Content-Disposition", "")
            if "filename=" in content_disp:
                # Extract filename from header
                filename = content_disp.split("filename=")[1].strip('"\'')
                print(f"DOWNLOAD: Got filename from Content-Disposition: {filename}")
            
            # Also check Content-Type header for file type hints
            content_type = head_response.headers.get("Content-Type", "").lower()
            if "7z" in content_type or "application/x-7z-compressed" in content_type:
                detected_ext = ".7z"
                print(f"DOWNLOAD: Content-Type suggests .7z file")
            elif "zip" in content_type or "application/zip" in content_type:
                detected_ext = ".zip"
                print(f"DOWNLOAD: Content-Type suggests .zip file")
    except requests.exceptions.Timeout:
        print(f"DOWNLOAD: HEAD request timed out, will check actual file type")
    except requests.exceptions.RequestException as e:
        print(f"DOWNLOAD: HEAD request failed: {e}, will check actual file type")
    except Exception as e:
        print(f"DOWNLOAD: Error getting filename from HEAD request: {e}, will check actual file type")
    
    # Try to get actual file type by downloading first few bytes
    if is_vimm_download:
        try:
            print(f"DOWNLOAD: Checking actual file type by downloading first 6 bytes...")
            type_check_response = requests.get(download_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Range": "bytes=0-5"  # Only get first 6 bytes
            }, allow_redirects=True, timeout=5, stream=True)
            if type_check_response.status_code in (200, 206):  # 206 = Partial Content
                magic_bytes = type_check_response.content[:6]
                type_check_response.close()
                
                # Check file signatures
                if len(magic_bytes) >= 2:
                    # 7z file signature: 37 7A BC AF 27 1C
                    if magic_bytes[:2] == b'7z' or (len(magic_bytes) >= 6 and magic_bytes[:6] == b'\x37\x7a\xbc\xaf\x27\x1c'):
                        actual_file_type = ".7z"
                        print(f"DOWNLOAD: ✓ Detected actual file type: 7z (from magic bytes)")
                    # ZIP file signature: 50 4B (PK)
                    elif magic_bytes[:2] == b'PK':
                        actual_file_type = ".zip"
                        print(f"DOWNLOAD: ✓ Detected actual file type: zip (from magic bytes)")
                    else:
                        print(f"DOWNLOAD: Could not determine file type from magic bytes: {magic_bytes.hex()}")
        except Exception as type_check_error:
            print(f"DOWNLOAD: Could not check actual file type: {type_check_error}")
    
    # Use actual file type if detected, otherwise use Content-Type hint, otherwise default
    if actual_file_type:
        detected_ext = actual_file_type
    elif not detected_ext:
        detected_ext = None
    
    # Fallback: use title + extension
    if not filename:
        # Try to get extension from URL first
        ext = os.path.splitext(download_url.split("?")[0])[1]
        # If we detected an extension (from actual file or Content-Type), use that
        if detected_ext:
            ext = detected_ext
        # Default to .zip if no extension found
        if not ext:
            ext = ".zip"
        filename = f"{safe_title}{ext}"
        print(f"DOWNLOAD: Using fallback filename: {filename}")
    elif detected_ext:
        # We have a detected extension - check if filename extension matches
        current_ext = os.path.splitext(filename)[1].lower()
        if current_ext != detected_ext.lower():
            # Extension doesn't match, update it
            file_base = os.path.splitext(filename)[0]
            filename = f"{file_base}{detected_ext}"
            print(f"DOWNLOAD: ✓ Updated filename extension from {current_ext} to {detected_ext} (detected from actual file)")
        else:
            print(f"DOWNLOAD: Filename extension {current_ext} matches detected type")
    
    file_path = os.path.join(download_dir, filename)
    
    # Check if file already exists
    if os.path.exists(file_path):
        flash(f"File already exists: {filename}", "error")
        return redirect(url_for("index"))
    
    # Use Aria2 for downloads (better progress tracking and resume support)
    try:
        print(f"DOWNLOAD: Adding to Aria2: {download_url} -> {filename}")
        
        # Check if there are already active downloads from vimm.net
        if is_vimm_download:
            # Check for active downloads from vimm.net
            try:
                # Get all active downloads
                active_downloads = aria2_rpc_call("aria2.tellActive", [])
                
                # Check if any active download is from vimm.net
                for dl in active_downloads:
                    dl_files = dl.get("files", [])
                    for file_info in dl_files:
                        dl_uri = file_info.get("uris", [{}])[0].get("uri", "")
                        if "vimm.net" in dl_uri.lower() or "dl3.vimm.net" in dl_uri.lower():
                            # There's already a vimm.net download in progress
                            flash("Download not started: Vimm's Lair only allows one download at a time. Please wait for the current download to finish.", "error")
                            return redirect(url_for("index"))
            except Exception as check_error:
                # If we can't check, continue anyway (might be a temporary Aria2 issue)
                print(f"DOWNLOAD: Could not check for active downloads: {check_error}")
        
        # For vimm.net downloads, always start with dl1 and try sequentially through dl20
        # Extract mediaId from any format and always start with dl1
        if is_vimm_download:
            import re
            media_id = None
            
            # Extract mediaId from URL (handles all formats)
            if download_url.startswith("MEDIAID:"):
                media_id = download_url.replace("MEDIAID:", "").strip()
            elif download_url.startswith("?mediaId="):
                media_id = download_url.replace("?mediaId=", "").strip()
            else:
                # Extract from full URL like https://dl3.vimm.net/?mediaId=63374
                media_id_match = re.search(r'mediaId=(\d+)', download_url)
                if media_id_match:
                    media_id = media_id_match.group(1)
            
            if media_id:
                # Always start with dl1, regardless of what URL was provided
                download_url = f"https://dl1.vimm.net/?mediaId={media_id}"
                # Store mediaId and current server for error handling
                download_url_with_media = {"url": download_url, "mediaId": media_id, "current_server": 1}
                print(f"DOWNLOAD: Extracted mediaId {media_id}, starting with dl1: {download_url}")
            else:
                # Can't extract mediaId, just use the provided URL
                print(f"DOWNLOAD: Could not extract mediaId, using provided URL: {download_url}")
                download_url_with_media = None
        
        # Prepare options for Aria2
        options = {
            "dir": download_dir,
            "out": filename,
        }
        
        # Add headers for vimm.net
        headers = []
        if game_page_url:
            headers.append(f"Referer: {game_page_url}")
        headers.append("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        headers.append("Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        headers.append("Accept-Language: en-US,en;q=0.5")
        headers.append("Connection: keep-alive")
        headers.append("Upgrade-Insecure-Requests: 1")
        
        if headers:
            options["header"] = headers
        
        # Add download to Aria2
        params = [
            [download_url],
            options
        ]
        gid = aria2_rpc_call("aria2.addUri", params)
        print(f"DOWNLOAD: Aria2 GID: {gid}")
        
        # Check status after a delay to catch immediate errors (for all downloads, not just vimm.net)
        import time
        time.sleep(3)  # Wait 3 seconds for download to start or fail
        
        try:
            # Check the status of the download we just added
            status = aria2_rpc_call("aria2.tellStatus", [gid])
            status_str = status.get("status", "")
            error_code = status.get("errorCode", "")
            error_message = status.get("errorMessage", "")
            
            # Check if download failed with an error
            if status_str == "error" or error_code:
                error_msg = error_message or f"Error code: {error_code}" or "Unknown error"
                error_msg_lower = error_msg.lower()
                
                # Log detailed error information
                print(f"DOWNLOAD: Error details - Status: {status_str}, Code: {error_code}, Message: {error_message}")
                print(f"DOWNLOAD: Full status response: {status}")
                
                # Remove the failed download from Aria2
                try:
                    aria2_rpc_call("aria2.remove", [gid])
                except:
                    pass
                
                # Check for various errors that indicate we should try the next server
                # Error codes: 3=Resource not found, 19=DNS error, 22=HTTP error (429, etc.)
                if is_vimm_download and download_url_with_media and download_url_with_media.get("mediaId"):
                    should_retry = False
                    retry_reason = ""
                    
                    if error_code == "3" or "resource not found" in error_msg_lower:
                        should_retry = True
                        retry_reason = "Resource not found"
                    elif error_code == "19" or "dns" in error_msg_lower or "name resolution" in error_msg_lower:
                        should_retry = True
                        retry_reason = "DNS resolution failed"
                    elif "429" in error_msg or (error_code == "22" and "429" in error_msg):
                        should_retry = True
                        retry_reason = "Rate limited (429)"
                    elif error_code == "22" and ("not found" in error_msg_lower or "404" in error_msg_lower):
                        should_retry = True
                        retry_reason = "HTTP 404 Not found"
                    
                    if should_retry:
                        media_id = download_url_with_media["mediaId"]
                        current_server = download_url_with_media.get("current_server", 0)
                        
                        print(f"DOWNLOAD: Got {retry_reason} error on dl{current_server} (error code: {error_code}), trying next server...")
                        print(f"DOWNLOAD: Will try servers dl{current_server + 1} through dl20 sequentially")
                        
                        # Try next server (current_server + 1 through dl20)
                        import time
                        validation_headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.5",
                            "Connection": "keep-alive",
                            "Upgrade-Insecure-Requests": "1",
                        }
                        if game_page_url:
                            validation_headers["Referer"] = game_page_url
                        
                        working_server = None
                        servers_tried = []
                        # Try next servers sequentially (current_server + 1 through dl20)
                        for server_num in range(current_server + 1, 21):
                            test_url = f"https://dl{server_num}.vimm.net/?mediaId={media_id}"
                            attempt_num = server_num - current_server
                            total_attempts = 20 - current_server
                            print(f"DOWNLOAD: [Attempt {attempt_num}/{total_attempts}] Trying dl{server_num}.vimm.net...")
                            servers_tried.append(server_num)
                            
                            # Add 1 second delay between attempts to avoid triggering "only 1 download" limit
                            if server_num > current_server + 1:
                                print(f"DOWNLOAD: Waiting 1 second before trying dl{server_num}...")
                                time.sleep(1)
                            
                            try:
                                # Try HEAD first
                                try:
                                    test_response = requests.head(test_url, headers=validation_headers, allow_redirects=True, timeout=3, stream=False)
                                except:
                                    test_response = requests.get(test_url, headers=validation_headers, allow_redirects=True, timeout=3, stream=True)
                                    test_response.close()
                                
                                # Check if this server works
                                print(f"DOWNLOAD: dl{server_num} responded with HTTP {test_response.status_code}")
                                if test_response.status_code in (200, 301, 302, 303, 307, 308):
                                    working_server = f"dl{server_num}.vimm.net"
                                    # Retry download with new server
                                    print(f"DOWNLOAD: ✓ dl{server_num} looks good (HTTP {test_response.status_code}), adding to Aria2...")
                                    
                                    # Add download to Aria2 with new URL
                                    retry_options = {
                                        "dir": download_dir,
                                        "out": filename,
                                    }
                                    retry_headers = []
                                    if game_page_url:
                                        retry_headers.append(f"Referer: {game_page_url}")
                                    retry_headers.append("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                                    retry_headers.append("Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
                                    retry_headers.append("Accept-Language: en-US,en;q=0.5")
                                    retry_headers.append("Connection: keep-alive")
                                    retry_headers.append("Upgrade-Insecure-Requests: 1")
                                    if retry_headers:
                                        retry_options["header"] = retry_headers
                                    
                                    retry_params = [[test_url], retry_options]
                                    new_gid = aria2_rpc_call("aria2.addUri", retry_params)
                                    print(f"DOWNLOAD: ✓ Added dl{server_num} to Aria2, GID: {new_gid}")
                                    
                                    # Update current_server for potential future retries
                                    download_url_with_media["current_server"] = server_num
                                    
                                    # Wait a moment and check if this one works
                                    print(f"DOWNLOAD: Waiting 3 seconds to check if dl{server_num} download started successfully...")
                                    time.sleep(3)
                                    try:
                                        retry_status = aria2_rpc_call("aria2.tellStatus", [new_gid])
                                        retry_status_str = retry_status.get("status", "")
                                        retry_error_code = retry_status.get("errorCode", "")
                                        retry_error_msg = retry_status.get("errorMessage", "")
                                        
                                        print(f"DOWNLOAD: dl{server_num} status check - Status: {retry_status_str}, Error Code: {retry_error_code}")
                                        
                                        if retry_status_str == "error" or retry_error_code:
                                            # This server also failed, continue to next
                                            print(f"DOWNLOAD: ✗ dl{server_num} failed with error code {retry_error_code}: {retry_error_msg}")
                                            print(f"DOWNLOAD: Removing failed download and trying next server...")
                                            try:
                                                aria2_rpc_call("aria2.remove", [new_gid])
                                            except:
                                                pass
                                            continue
                                        else:
                                            # Success!
                                            print(f"DOWNLOAD: ✓ dl{server_num} download started successfully!")
                                            flash(f"Download started with server {working_server}: {title}. <a href='/aria2' target='_blank'>View progress</a>.", "ok")
                                            return redirect(url_for("index"))
                                    except Exception as status_check_error:
                                        # Can't check status, assume it's working
                                        print(f"DOWNLOAD: Could not check dl{server_num} status ({status_check_error}), assuming it's working...")
                                        flash(f"Download started with server {working_server}: {title}. <a href='/aria2' target='_blank'>View progress</a>.", "ok")
                                        return redirect(url_for("index"))
                                elif test_response.status_code == 429:
                                    # This server also rate-limited, continue to next
                                    print(f"DOWNLOAD: ✗ dl{server_num} rate-limited (429), trying next server...")
                                    continue
                                elif test_response.status_code == 404:
                                    # Resource not found on this server, try next
                                    print(f"DOWNLOAD: ✗ dl{server_num} returned 404 (not found), trying next server...")
                                    continue
                                else:
                                    # Other status, might work, try it anyway
                                    print(f"DOWNLOAD: dl{server_num} returned status {test_response.status_code}, trying anyway...")
                                    working_server = f"dl{server_num}.vimm.net"
                                    
                                    retry_options = {
                                        "dir": download_dir,
                                        "out": filename,
                                    }
                                    retry_headers = []
                                    if game_page_url:
                                        retry_headers.append(f"Referer: {game_page_url}")
                                    retry_headers.append("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                                    retry_headers.append("Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
                                    retry_headers.append("Accept-Language: en-US,en;q=0.5")
                                    retry_headers.append("Connection: keep-alive")
                                    retry_headers.append("Upgrade-Insecure-Requests: 1")
                                    if retry_headers:
                                        retry_options["header"] = retry_headers
                                    
                                    retry_params = [[test_url], retry_options]
                                    new_gid = aria2_rpc_call("aria2.addUri", retry_params)
                                    print(f"DOWNLOAD: Retried with server {working_server}, new GID: {new_gid}")
                                    
                                    download_url_with_media["current_server"] = server_num
                                    
                                    time.sleep(3)
                                    try:
                                        retry_status = aria2_rpc_call("aria2.tellStatus", [new_gid])
                                        retry_status_str = retry_status.get("status", "")
                                        retry_error_code = retry_status.get("errorCode", "")
                                        
                                        if retry_status_str == "error" or retry_error_code:
                                            print(f"DOWNLOAD: dl{server_num} also failed, trying next server...")
                                            try:
                                                aria2_rpc_call("aria2.remove", [new_gid])
                                            except:
                                                pass
                                            continue
                                        else:
                                            flash(f"Download started with server {working_server}: {title}. <a href='/aria2' target='_blank'>View progress</a>.", "ok")
                                            return redirect(url_for("index"))
                                    except:
                                        flash(f"Download started with server {working_server}: {title}. <a href='/aria2' target='_blank'>View progress</a>.", "ok")
                                        return redirect(url_for("index"))
                            except requests.exceptions.RequestException as e:
                                print(f"DOWNLOAD: ✗ dl{server_num} connection error: {e}, trying next server...")
                                continue
                            except Exception as e:
                                print(f"DOWNLOAD: ✗ dl{server_num} unexpected error: {e}, trying next server...")
                                continue
                        
                        if not working_server:
                            servers_tried_str = ", ".join([f"dl{i}" for i in servers_tried])
                            print(f"DOWNLOAD: ✗ All servers failed. Tried: {servers_tried_str}")
                            flash(f"Download failed: Tried servers {servers_tried_str} but none worked. Original error: {retry_reason}. The file may be unavailable or all servers are experiencing issues.", "error")
                            return redirect(url_for("index"))
                        return redirect(url_for("index"))
                
                # Check for 429 (rate limit) - try other servers if we have mediaId (old code path)
                if is_vimm_download and ("429" in error_msg or (error_code == "22" and "429" in error_msg)):
                    # Rate limited - try other servers if we have mediaId
                    if download_url_with_media and download_url_with_media.get("mediaId"):
                        media_id = download_url_with_media["mediaId"]
                        print(f"DOWNLOAD: Got 429 error, trying other servers (dl1-dl20) for mediaId {media_id}...")
                        
                        # Extract current server number to skip it
                        import re
                        current_server_match = re.search(r'//dl(\d+)\.vimm\.net', download_url)
                        current_server_num = int(current_server_match.group(1)) if current_server_match else None
                        
                        validation_headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.5",
                            "Connection": "keep-alive",
                            "Upgrade-Insecure-Requests": "1",
                        }
                        if game_page_url:
                            validation_headers["Referer"] = game_page_url
                        
                        working_server = None
                        import time
                        # Try all servers except the one that failed
                        for server_num in range(1, 21):
                            if current_server_num and server_num == current_server_num:
                                continue  # Skip the server that already failed
                            
                            test_url = f"https://dl{server_num}.vimm.net/?mediaId={media_id}"
                            print(f"DOWNLOAD: Trying alternative server: {test_url}...")
                            try:
                                time.sleep(0.5)  # Small delay to avoid rate limiting
                                
                                # Try HEAD first
                                try:
                                    test_response = requests.head(test_url, headers=validation_headers, allow_redirects=True, timeout=3, stream=False)
                                except:
                                    test_response = requests.get(test_url, headers=validation_headers, allow_redirects=True, timeout=3, stream=True)
                                    test_response.close()
                                
                                # Check if this server works
                                if test_response.status_code in (200, 301, 302, 303, 307, 308):
                                    working_server = f"dl{server_num}.vimm.net"
                                    # Retry download with new server
                                    print(f"DOWNLOAD: Found working alternative server: {working_server}, retrying download...")
                                    
                                    # Add download to Aria2 with new URL
                                    retry_options = {
                                        "dir": download_dir,
                                        "out": filename,
                                    }
                                    retry_headers = []
                                    if game_page_url:
                                        retry_headers.append(f"Referer: {game_page_url}")
                                    retry_headers.append("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                                    retry_headers.append("Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
                                    retry_headers.append("Accept-Language: en-US,en;q=0.5")
                                    retry_headers.append("Connection: keep-alive")
                                    retry_headers.append("Upgrade-Insecure-Requests: 1")
                                    if retry_headers:
                                        retry_options["header"] = retry_headers
                                    
                                    retry_params = [[test_url], retry_options]
                                    new_gid = aria2_rpc_call("aria2.addUri", retry_params)
                                    print(f"DOWNLOAD: Retried with server {working_server}, new GID: {new_gid}")
                                    flash(f"Download started with alternative server ({working_server}): {title}. <a href='/aria2' target='_blank'>View progress</a>.", "ok")
                                    return redirect(url_for("index"))
                                elif test_response.status_code == 429:
                                    # This server also rate-limited, continue
                                    continue
                                else:
                                    # Other status, might work, try it anyway
                                    working_server = f"dl{server_num}.vimm.net"
                                    # Retry download with new server
                                    print(f"DOWNLOAD: Trying alternative server {working_server} despite status {test_response.status_code}...")
                                    
                                    retry_options = {
                                        "dir": download_dir,
                                        "out": filename,
                                    }
                                    retry_headers = []
                                    if game_page_url:
                                        retry_headers.append(f"Referer: {game_page_url}")
                                    retry_headers.append("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                                    retry_headers.append("Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
                                    retry_headers.append("Accept-Language: en-US,en;q=0.5")
                                    retry_headers.append("Connection: keep-alive")
                                    retry_headers.append("Upgrade-Insecure-Requests: 1")
                                    if retry_headers:
                                        retry_options["header"] = retry_headers
                                    
                                    retry_params = [[test_url], retry_options]
                                    new_gid = aria2_rpc_call("aria2.addUri", retry_params)
                                    print(f"DOWNLOAD: Retried with server {working_server}, new GID: {new_gid}")
                                    flash(f"Download started with alternative server ({working_server}): {title}. <a href='/aria2' target='_blank'>View progress</a>.", "ok")
                                    return redirect(url_for("index"))
                            except Exception as e:
                                print(f"DOWNLOAD: Error trying alternative server dl{server_num}: {e}")
                                continue
                        
                        if not working_server:
                            flash(f"Download failed: All download servers (dl1-dl20) returned rate limit errors (429). Please wait a few minutes and try again.", "error")
                            return redirect(url_for("index"))
                    else:
                        flash(f"Download failed: Rate limited (429). The server is temporarily blocking requests. Please wait a few minutes and try again.", "error")
                        return redirect(url_for("index"))
                # Check for common vimm.net concurrent download error messages
                elif is_vimm_download and any(keyword in error_msg_lower for keyword in ["already", "one at a time", "concurrent", "wait", "busy", "403"]):
                    flash("Download not started: Vimm's Lair only allows one download at a time. Please wait for the current download to finish.", "error")
                elif "resource not found" in error_msg_lower or error_code in ["3", "4"]:
                    # Aria2 error code 3 = RESOURCE_NOT_FOUND, 4 = FILE_NOT_FOUND
                    print(f"DOWNLOAD: Resource not found error - URL may be invalid or expired")
                    flash(f"Download failed: Resource not found. The file may have been removed, the URL may be expired, or the server may be blocking the request. Try searching again to get a fresh download link.", "error")
                else:
                    # Generic error message for other failures
                    print(f"DOWNLOAD: Download failed immediately: {error_msg}")
                    flash(f"Download failed: {error_msg}. The file may be too large, unavailable, or the server may be experiencing issues.", "error")
                return redirect(url_for("index"))
            
            # Log successful start
            print(f"DOWNLOAD: Download started successfully - Status: {status_str}, GID: {gid}")
        except Exception as status_error:
            # If we can't check status, log it but continue (might be a temporary Aria2 issue)
            print(f"DOWNLOAD: Could not check download status: {status_error}")
            print(f"DOWNLOAD: Assuming download started successfully (GID: {gid})")
        
        flash(f"Download started: {title}. <a href='/aria2' target='_blank'>View progress</a>.", "ok")
    except Exception as e:
        print(f"DOWNLOAD: Failed to add to Aria2: {e}")
        import traceback
        print(f"DOWNLOAD: Traceback:\n{traceback.format_exc()}")
        
        # Check if error message indicates concurrent download issue
        error_str = str(e).lower()
        if any(keyword in error_str for keyword in ["already", "one at a time", "concurrent", "wait", "busy", "403", "429"]):
            flash("Download not started: Vimm's Lair only allows one download at a time. Please wait for the current download to finish.", "error")
        else:
            flash(f"Failed to start download: {e}. Make sure Aria2 is running.", "error")
    
    return redirect(url_for("index"))


# Queue processing function
def process_queue():
    """Background thread to process download queue"""
    global queue_processing, download_queue
    
    while queue_processing:
        try:
            with queue_lock:
                if not download_queue:
                    queue_processing = False
                    print("QUEUE: Queue empty, stopping processing")
                    break
                
                # Find first incomplete item (skip items with errors)
                current_item = None
                for item in download_queue:
                    if (not item.get("completed", False) 
                        and not item.get("downloading", False) 
                        and not item.get("error")):  # Skip items that have errors
                        current_item = item
                        break
                
                if not current_item:
                    # All items are either completed or downloading
                    # Check if any are still downloading
                    any_downloading = False
                    for item in download_queue:
                        if item.get("downloading", False):
                            gid = item.get("gid")
                            if gid:
                                # Check if download is still active
                                try:
                                    status = aria2_rpc_call("aria2.tellStatus", [gid])
                                    if status.get("status") not in ("complete", "error", "removed"):
                                        any_downloading = True
                                        break
                                    elif status.get("status") == "complete":
                                        # Mark as completed if file exists
                                        file_path = item.get("file_path")
                                        if file_path and os.path.exists(file_path):
                                            item["completed"] = True
                                            item["downloading"] = False
                                            save_queue()
                                    elif status.get("status") == "error":
                                        item["downloading"] = False
                                        item["error"] = status.get("errorMessage", "Unknown error")
                                        item["completed"] = True  # Mark as completed so it's skipped
                                        save_queue()
                                except:
                                    # Download might have been removed, mark as not downloading
                                    item["downloading"] = False
                                    save_queue()
                    
                    if not any_downloading:
                        queue_processing = False
                        print("QUEUE: All items completed, stopping processing")
                    break
            
            if current_item:
                # Mark as downloading - need to update in the list
                with queue_lock:
                    # Find and update the item in the list
                    for idx, item in enumerate(download_queue):
                        if item.get("download_url") == current_item.get("download_url"):
                            download_queue[idx]["downloading"] = True
                            download_queue[idx]["gid"] = None
                            current_item = download_queue[idx]  # Update reference
                            break
                save_queue()
                
                # Start the download
                download_url = current_item["download_url"]
                title = current_item["title"]
                game_page_url = current_item.get("game_page_url", "")
                
                download_dir = cfg.get("downloads", {}).get("directory", "/opt/rompi/downloads")
                os.makedirs(download_dir, exist_ok=True)
                
                safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.')).rstrip()
                if not safe_title:
                    safe_title = "download"
                
                # For vimm.net downloads, extract mediaId and always start with dl1
                is_vimm_download = "vimm.net" in download_url.lower() or "dl" in download_url.lower() and "vimm.net" in download_url.lower()
                download_url_with_media = None
                
                if is_vimm_download:
                    import re
                    media_id = None
                    
                    # Extract mediaId from URL (handles all formats)
                    if download_url.startswith("MEDIAID:"):
                        media_id = download_url.replace("MEDIAID:", "").strip()
                    elif download_url.startswith("?mediaId="):
                        media_id = download_url.replace("?mediaId=", "").strip()
                    else:
                        # Extract from full URL like https://dl3.vimm.net/?mediaId=63374
                        media_id_match = re.search(r'mediaId=(\d+)', download_url)
                        if media_id_match:
                            media_id = media_id_match.group(1)
                    
                    if media_id:
                        # Always start with dl1, regardless of what URL was provided
                        download_url = f"https://dl1.vimm.net/?mediaId={media_id}"
                        # Store mediaId and current server for error handling
                        download_url_with_media = {"url": download_url, "mediaId": media_id, "current_server": 1}
                        print(f"QUEUE: Extracted mediaId {media_id}, starting with dl1: {download_url}")
                
                filename = None
                detected_ext = None
                actual_file_type = None
                
                try:
                    # Make HEAD request to get filename from Content-Disposition header
                    # Use a shorter timeout and catch exceptions gracefully
                    head_response = requests.head(download_url, allow_redirects=True, timeout=5, stream=False)
                    if head_response.status_code == 200:
                        content_disp = head_response.headers.get("Content-Disposition", "")
                        if "filename=" in content_disp:
                            filename = content_disp.split("filename=")[1].strip('"\'')
                            print(f"QUEUE: Got filename from Content-Disposition: {filename}")
                        
                        # Also check Content-Type header for file type hints
                        content_type = head_response.headers.get("Content-Type", "").lower()
                        if "7z" in content_type or "application/x-7z-compressed" in content_type:
                            detected_ext = ".7z"
                            print(f"QUEUE: Content-Type suggests .7z file")
                        elif "zip" in content_type or "application/zip" in content_type:
                            detected_ext = ".zip"
                            print(f"QUEUE: Content-Type suggests .zip file")
                except requests.exceptions.Timeout:
                    print(f"QUEUE: HEAD request timed out, will check actual file type")
                except requests.exceptions.RequestException as e:
                    print(f"QUEUE: HEAD request failed: {e}, will check actual file type")
                except Exception as e:
                    print(f"QUEUE: Error getting filename from HEAD request: {e}, will check actual file type")
                
                # Try to get actual file type by downloading first few bytes
                if is_vimm_download:
                    try:
                        print(f"QUEUE: Checking actual file type by downloading first 6 bytes...")
                        type_check_headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        }
                        if game_page_url:
                            type_check_headers["Referer"] = game_page_url
                        type_check_response = requests.get(download_url, headers=type_check_headers, 
                                                          allow_redirects=True, timeout=5, stream=True)
                        # Read only first 6 bytes
                        magic_bytes = b''
                        for chunk in type_check_response.iter_content(chunk_size=6):
                            magic_bytes = chunk[:6]
                            break
                        type_check_response.close()
                        
                        # Check file signatures
                        if len(magic_bytes) >= 2:
                            # 7z file signature: 37 7A BC AF 27 1C
                            if magic_bytes[:2] == b'7z' or (len(magic_bytes) >= 6 and magic_bytes[:6] == b'\x37\x7a\xbc\xaf\x27\x1c'):
                                actual_file_type = ".7z"
                                print(f"QUEUE: ✓ Detected actual file type: 7z (from magic bytes)")
                            # ZIP file signature: 50 4B (PK)
                            elif magic_bytes[:2] == b'PK':
                                actual_file_type = ".zip"
                                print(f"QUEUE: ✓ Detected actual file type: zip (from magic bytes)")
                            else:
                                print(f"QUEUE: Could not determine file type from magic bytes: {magic_bytes.hex()}")
                    except Exception as type_check_error:
                        print(f"QUEUE: Could not check actual file type: {type_check_error}")
                
                # Use actual file type if detected, otherwise use Content-Type hint, otherwise default
                if actual_file_type:
                    detected_ext = actual_file_type
                elif not detected_ext:
                    detected_ext = None
                
                # Fallback: use title + extension
                if not filename:
                    # Try to get extension from URL first
                    ext = os.path.splitext(download_url.split("?")[0])[1]
                    # If we detected an extension (from actual file or Content-Type), use that
                    if detected_ext:
                        ext = detected_ext
                    # Default to .zip if no extension found
                    if not ext:
                        ext = ".zip"
                    filename = f"{safe_title}{ext}"
                    print(f"QUEUE: Using fallback filename: {filename}")
                elif detected_ext:
                    # We have a detected extension - check if filename extension matches
                    current_ext = os.path.splitext(filename)[1].lower()
                    if current_ext != detected_ext.lower():
                        # Extension doesn't match, update it
                        file_base = os.path.splitext(filename)[0]
                        filename = f"{file_base}{detected_ext}"
                        print(f"QUEUE: ✓ Updated filename extension from {current_ext} to {detected_ext} (detected from actual file)")
                    else:
                        print(f"QUEUE: Filename extension {current_ext} matches detected type")
                
                file_path = os.path.join(download_dir, filename)
                current_item["filename"] = filename
                current_item["file_path"] = file_path
                
                # Prepare Aria2 options
                options = {
                    "dir": download_dir,
                    "out": filename,
                }
                
                headers = []
                if game_page_url:
                    headers.append(f"Referer: {game_page_url}")
                headers.append("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                headers.append("Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
                headers.append("Accept-Language: en-US,en;q=0.5")
                headers.append("Connection: keep-alive")
                headers.append("Upgrade-Insecure-Requests: 1")
                
                if headers:
                    options["header"] = headers
                
                # Add to Aria2
                try:
                    params = [[download_url], options]
                    gid = aria2_rpc_call("aria2.addUri", params)
                    
                    with queue_lock:
                        # Update GID in the list
                        for idx, item in enumerate(download_queue):
                            if item.get("download_url") == current_item.get("download_url"):
                                download_queue[idx]["gid"] = gid
                                current_item = download_queue[idx]  # Update reference
                                break
                    save_queue()
                    
                    print(f"QUEUE: Started download {title} (GID: {gid})")
                    
                    # Check for immediate errors (like "Resource not found", DNS errors, etc.)
                    time.sleep(3)  # Wait a moment for Aria2 to process
                    try:
                        initial_status = aria2_rpc_call("aria2.tellStatus", [gid])
                        status_str = initial_status.get("status", "")
                        error_code = initial_status.get("errorCode", "")
                        error_message = initial_status.get("errorMessage", "")
                        
                        if status_str == "error" or error_code:
                            error_msg = error_message or f"Error code: {error_code}" or "Unknown error"
                            error_msg_lower = error_msg.lower()
                            
                            # Check if we should retry with next server (same logic as direct download)
                            should_retry = False
                            retry_reason = ""
                            
                            if is_vimm_download and download_url_with_media and download_url_with_media.get("mediaId"):
                                if error_code == "3" or "resource not found" in error_msg_lower:
                                    should_retry = True
                                    retry_reason = "Resource not found"
                                elif error_code == "19" or "dns" in error_msg_lower or "name resolution" in error_msg_lower:
                                    should_retry = True
                                    retry_reason = "DNS resolution failed"
                                elif "429" in error_msg or (error_code == "22" and "429" in error_msg):
                                    should_retry = True
                                    retry_reason = "Rate limited (429)"
                                elif error_code == "22" and ("not found" in error_msg_lower or "404" in error_msg_lower):
                                    should_retry = True
                                    retry_reason = "HTTP 404 Not found"
                                
                                if should_retry:
                                    media_id = download_url_with_media["mediaId"]
                                    current_server = download_url_with_media.get("current_server", 0)
                                    
                                    print(f"QUEUE: Got {retry_reason} error on dl{current_server} (error code: {error_code}), trying next server...")
                                    print(f"QUEUE: Will try servers dl{current_server + 1} through dl20 sequentially")
                                    
                                    # Remove failed download
                                    try:
                                        aria2_rpc_call("aria2.remove", [gid])
                                    except:
                                        pass
                                    
                                    # Try next servers sequentially
                                    validation_headers = {
                                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                        "Accept-Language": "en-US,en;q=0.5",
                                        "Connection": "keep-alive",
                                        "Upgrade-Insecure-Requests": "1",
                                    }
                                    if game_page_url:
                                        validation_headers["Referer"] = game_page_url
                                    
                                    working_server = None
                                    servers_tried = []
                                    # Try next servers sequentially (current_server + 1 through dl20)
                                    for server_num in range(current_server + 1, 21):
                                        test_url = f"https://dl{server_num}.vimm.net/?mediaId={media_id}"
                                        attempt_num = server_num - current_server
                                        total_attempts = 20 - current_server
                                        print(f"QUEUE: [Attempt {attempt_num}/{total_attempts}] Trying dl{server_num}.vimm.net...")
                                        servers_tried.append(server_num)
                                        
                                        # Add 1 second delay between attempts
                                        if server_num > current_server + 1:
                                            print(f"QUEUE: Waiting 1 second before trying dl{server_num}...")
                                            time.sleep(1)
                                        
                                        try:
                                            # Try HEAD first
                                            try:
                                                test_response = requests.head(test_url, headers=validation_headers, allow_redirects=True, timeout=3, stream=False)
                                            except:
                                                test_response = requests.get(test_url, headers=validation_headers, allow_redirects=True, timeout=3, stream=True)
                                                test_response.close()
                                            
                                            # Check if this server works
                                            print(f"QUEUE: dl{server_num} responded with HTTP {test_response.status_code}")
                                            if test_response.status_code in (200, 301, 302, 303, 307, 308):
                                                working_server = f"dl{server_num}.vimm.net"
                                                print(f"QUEUE: ✓ dl{server_num} looks good (HTTP {test_response.status_code}), adding to Aria2...")
                                                
                                                # Add download to Aria2 with new URL
                                                retry_options = {
                                                    "dir": download_dir,
                                                    "out": filename,
                                                }
                                                retry_headers = []
                                                if game_page_url:
                                                    retry_headers.append(f"Referer: {game_page_url}")
                                                retry_headers.append("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                                                retry_headers.append("Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
                                                retry_headers.append("Accept-Language: en-US,en;q=0.5")
                                                retry_headers.append("Connection: keep-alive")
                                                retry_headers.append("Upgrade-Insecure-Requests: 1")
                                                if retry_headers:
                                                    retry_options["header"] = retry_headers
                                                
                                                retry_params = [[test_url], retry_options]
                                                new_gid = aria2_rpc_call("aria2.addUri", retry_params)
                                                print(f"QUEUE: ✓ Added dl{server_num} to Aria2, GID: {new_gid}")
                                                
                                                # Update GID and current_server in queue
                                                with queue_lock:
                                                    for idx, item in enumerate(download_queue):
                                                        if item.get("download_url") == current_item.get("download_url"):
                                                            download_queue[idx]["gid"] = new_gid
                                                            download_queue[idx]["download_url"] = test_url  # Update URL to working server
                                                            current_item = download_queue[idx]
                                                            break
                                                save_queue()
                                                
                                                # Wait and check if this one works
                                                print(f"QUEUE: Waiting 3 seconds to check if dl{server_num} download started successfully...")
                                                time.sleep(3)
                                                try:
                                                    retry_status = aria2_rpc_call("aria2.tellStatus", [new_gid])
                                                    retry_status_str = retry_status.get("status", "")
                                                    retry_error_code = retry_status.get("errorCode", "")
                                                    retry_error_msg = retry_status.get("errorMessage", "")
                                                    
                                                    print(f"QUEUE: dl{server_num} status check - Status: {retry_status_str}, Error Code: {retry_error_code}")
                                                    
                                                    if retry_status_str == "error" or retry_error_code:
                                                        # This server also failed, continue to next
                                                        print(f"QUEUE: ✗ dl{server_num} failed with error code {retry_error_code}: {retry_error_msg}")
                                                        print(f"QUEUE: Removing failed download and trying next server...")
                                                        try:
                                                            aria2_rpc_call("aria2.remove", [new_gid])
                                                        except:
                                                            pass
                                                        download_url_with_media["current_server"] = server_num
                                                        continue
                                                    else:
                                                        # Success!
                                                        print(f"QUEUE: ✓ dl{server_num} download started successfully!")
                                                        gid = new_gid  # Update gid for monitoring loop
                                                        break  # Break out of server retry loop, continue to monitoring
                                                except Exception as status_check_error:
                                                    # Can't check status, assume it's working
                                                    print(f"QUEUE: Could not check dl{server_num} status ({status_check_error}), assuming it's working...")
                                                    gid = new_gid  # Update gid for monitoring loop
                                                    break  # Break out of server retry loop, continue to monitoring
                                            elif test_response.status_code == 429:
                                                print(f"QUEUE: ✗ dl{server_num} rate-limited (429), trying next server...")
                                                continue
                                            elif test_response.status_code == 404:
                                                print(f"QUEUE: ✗ dl{server_num} returned 404 (not found), trying next server...")
                                                continue
                                            else:
                                                print(f"QUEUE: ✗ dl{server_num} returned unexpected status {test_response.status_code}, trying next server...")
                                                continue
                                        except requests.exceptions.RequestException as e:
                                            print(f"QUEUE: ✗ dl{server_num} connection error: {e}, trying next server...")
                                            continue
                                        except Exception as e:
                                            print(f"QUEUE: ✗ dl{server_num} unexpected error: {e}, trying next server...")
                                            continue
                                    
                                    if not working_server:
                                        servers_tried_str = ", ".join([f"dl{i}" for i in servers_tried])
                                        print(f"QUEUE: ✗ All servers failed. Tried: {servers_tried_str}")
                                        error_msg = f"Tried servers {servers_tried_str} but none worked. Original error: {retry_reason}"
                                        with queue_lock:
                                            for idx, item in enumerate(download_queue):
                                                if item.get("download_url") == current_item.get("download_url"):
                                                    download_queue[idx]["downloading"] = False
                                                    download_queue[idx]["error"] = error_msg
                                                    download_queue[idx]["completed"] = True
                                                    break
                                        save_queue()
                                        print(f"QUEUE: Download failed {title}: {error_msg} - skipping to next item")
                                        continue  # Skip to next item in queue
                                    # If we found a working server, continue to monitoring loop below
                                else:
                                    # Not a retry-able error, mark as failed
                                    with queue_lock:
                                        for idx, item in enumerate(download_queue):
                                            if item.get("gid") == gid:
                                                download_queue[idx]["downloading"] = False
                                                download_queue[idx]["error"] = error_msg
                                                download_queue[idx]["completed"] = True
                                                break
                                    save_queue()
                                    print(f"QUEUE: Download failed immediately {title}: {error_msg} - skipping to next item")
                                    continue  # Skip to next item in queue
                            else:
                                # Not a vimm.net download or no mediaId, just mark as failed
                                with queue_lock:
                                    for idx, item in enumerate(download_queue):
                                        if item.get("gid") == gid:
                                            download_queue[idx]["downloading"] = False
                                            download_queue[idx]["error"] = error_msg
                                            download_queue[idx]["completed"] = True
                                            break
                                save_queue()
                                print(f"QUEUE: Download failed immediately {title}: {error_msg} - skipping to next item")
                                continue  # Skip to next item in queue
                        else:
                            print(f"QUEUE: Download started successfully - Status: {status_str}, GID: {gid}")
                    except Exception as status_check_error:
                        # If we can't check status, log it but continue monitoring
                        print(f"QUEUE: Could not check initial status for {title}: {status_check_error}")
                        print(f"QUEUE: Assuming download started successfully (GID: {gid})")
                except Exception as add_error:
                    # Failed to add download to Aria2
                    error_msg = f"Failed to start download: {str(add_error)}"
                    with queue_lock:
                        for idx, item in enumerate(download_queue):
                            if item.get("download_url") == current_item.get("download_url"):
                                download_queue[idx]["downloading"] = False
                                download_queue[idx]["error"] = error_msg
                                download_queue[idx]["completed"] = True  # Mark as completed so it's skipped
                                break
                    save_queue()
                    print(f"QUEUE: Failed to add download {title}: {error_msg} - skipping to next item")
                    continue  # Skip to next item in queue
                
                # Monitor download until complete
                while True:
                    time.sleep(5)  # Check every 5 seconds
                    
                    if not queue_processing:
                        # Queue was stopped
                        break
                    
                    try:
                        status = aria2_rpc_call("aria2.tellStatus", [gid])
                        status_str = status.get("status", "")
                        
                        if status_str == "complete":
                            # Check if file exists
                            if os.path.exists(file_path):
                                # Check actual file type and rename if extension is wrong
                                try:
                                    with open(file_path, 'rb') as f:
                                        magic_bytes = f.read(6)
                                        actual_ext = None
                                        # 7z file signature: 37 7A BC AF 27 1C
                                        if magic_bytes[:2] == b'7z' or (len(magic_bytes) >= 6 and magic_bytes[:6] == b'\x37\x7a\xbc\xaf\x27\x1c'):
                                            actual_ext = ".7z"
                                            print(f"QUEUE: Detected 7z file signature")
                                        # ZIP file signature: 50 4B 03 04 or 50 4B 05 06 or 50 4B 07 08
                                        elif magic_bytes[:2] == b'PK':
                                            actual_ext = ".zip"
                                            print(f"QUEUE: Detected ZIP file signature")
                                        
                                        if actual_ext and not file_path.lower().endswith(actual_ext.lower()):
                                            # File extension doesn't match actual type, rename it
                                            new_file_path = os.path.splitext(file_path)[0] + actual_ext
                                            if not os.path.exists(new_file_path):
                                                os.rename(file_path, new_file_path)
                                                file_path = new_file_path
                                                filename = os.path.basename(new_file_path)
                                                print(f"QUEUE: ✓ Renamed file to correct extension: {filename} (was {os.path.basename(file_path)} before, detected {actual_ext})")
                                                # Update queue item with correct filename
                                                with queue_lock:
                                                    for idx, item in enumerate(download_queue):
                                                        if item.get("gid") == gid:
                                                            download_queue[idx]["filename"] = filename
                                                            download_queue[idx]["file_path"] = file_path
                                                            break
                                            else:
                                                print(f"QUEUE: Target file {new_file_path} already exists, keeping original name")
                                except Exception as rename_error:
                                    print(f"QUEUE: Could not check/rename file type: {rename_error}")
                                
                                with queue_lock:
                                    # Update in the list
                                    for idx, item in enumerate(download_queue):
                                        if item.get("gid") == gid:
                                            download_queue[idx]["completed"] = True
                                            download_queue[idx]["downloading"] = False
                                            break
                                save_queue()
                                print(f"QUEUE: Completed download {title}")
                                break
                        elif status_str == "error":
                            # Download failed - mark as completed (failed) so it's skipped
                            error_msg = status.get("errorMessage", "Unknown error")
                            with queue_lock:
                                # Update in the list
                                for idx, item in enumerate(download_queue):
                                    if item.get("gid") == gid:
                                        download_queue[idx]["downloading"] = False
                                        download_queue[idx]["error"] = error_msg
                                        download_queue[idx]["completed"] = True  # Mark as completed so it's skipped
                                        break
                            save_queue()
                            print(f"QUEUE: Download failed {title}: {error_msg} - skipping to next item")
                            break
                    except Exception as e:
                        print(f"QUEUE: Error checking status: {e}")
                        # Continue monitoring
                
        except Exception as e:
            print(f"QUEUE: Error in queue processing: {e}")
            import traceback
            traceback.print_exc()
            # Mark current item as failed so it's skipped
            if current_item:
                with queue_lock:
                    for idx, item in enumerate(download_queue):
                        if item.get("download_url") == current_item.get("download_url"):
                            download_queue[idx]["downloading"] = False
                            download_queue[idx]["error"] = f"Processing error: {str(e)}"
                            download_queue[idx]["completed"] = True  # Mark as completed so it's skipped
                            break
                save_queue()
                print(f"QUEUE: Marked {current_item.get('title', 'item')} as failed due to error - skipping to next item")
            time.sleep(2)  # Shorter delay before trying next item
        
        time.sleep(2)  # Small delay between items


@app.route("/queue/add", methods=["POST"])
def queue_add():
    """Add item to download queue"""
    # Support both form data and JSON
    if request.is_json:
        data = request.get_json()
        download_url = (data.get("download_url") or "").strip()
        title = (data.get("title") or "").strip()
        game_page_url = (data.get("game_page_url") or "").strip()
    else:
        download_url = (request.form.get("download_url") or "").strip()
        title = (request.form.get("title") or "").strip()
        game_page_url = (request.form.get("game_page_url") or "").strip()
    
    if not download_url or not download_url.startswith(("http://", "https://")):
        if request.is_json:
            return jsonify({"success": False, "error": "Invalid download URL"}), 400
        flash("Invalid download URL", "error")
        return redirect(url_for("index"))
    
    if not title:
        if request.is_json:
            return jsonify({"success": False, "error": "Title is required"}), 400
        flash("Title is required", "error")
        return redirect(url_for("index"))
    
    # Check if it's a vimm.net download
    is_vimm = "vimm.net" in download_url.lower() or "dl3.vimm.net" in download_url.lower()
    if not is_vimm:
        if request.is_json:
            return jsonify({"success": False, "error": "Queue is only for Vimm's Lair downloads"}), 400
        flash("Queue is only for Vimm's Lair downloads", "error")
        return redirect(url_for("index"))
    
    with queue_lock:
        if len(download_queue) >= MAX_QUEUE_SIZE:
            error_msg = f"Queue is full (maximum {MAX_QUEUE_SIZE} items). Please wait for downloads to complete or clear the queue."
            if request.is_json:
                return jsonify({"success": False, "error": error_msg}), 400
            flash(error_msg, "error")
            return redirect(url_for("index"))
        
        # Check if already in queue
        for item in download_queue:
            if item.get("download_url") == download_url:
                error_msg = f"'{title}' is already in the queue"
                if request.is_json:
                    return jsonify({"success": False, "error": error_msg}), 400
                flash(error_msg, "error")
                return redirect(url_for("index"))
        
        # Add to queue
        download_queue.append({
            "download_url": download_url,
            "title": title,
            "game_page_url": game_page_url,
            "completed": False,
            "downloading": False,
            "gid": None,
            "filename": "",
            "file_path": "",
            "error": None,
            "added_at": time.time()
        })
    
    save_queue()
    success_msg = f"Added '{title}' to download queue"
    if request.is_json:
        return jsonify({"success": True, "message": success_msg})
    flash(success_msg, "ok")
    return redirect(url_for("index"))


@app.route("/queue")
def queue_page():
    """Display download queue management page"""
    with queue_lock:
        queue_items = download_queue.copy()
        is_processing = queue_processing
    
    return render_template("queue.html", queue_items=queue_items, is_processing=is_processing, max_size=MAX_QUEUE_SIZE)


@app.route("/queue/start", methods=["POST"])
def queue_start():
    """Start processing the queue"""
    global queue_processing, queue_thread
    
    with queue_lock:
        if not download_queue:
            flash("Queue is empty", "error")
            return redirect(url_for("queue_page"))
        
        if queue_processing:
            flash("Queue is already processing", "error")
            return redirect(url_for("queue_page"))
        
        queue_processing = True
    
    # Start background thread if not running
    if queue_thread is None or not queue_thread.is_alive():
        queue_thread = threading.Thread(target=process_queue, daemon=True)
        queue_thread.start()
        print("QUEUE: Started queue processing thread")
    
    flash("Queue processing started", "ok")
    return redirect(url_for("queue_page"))


@app.route("/queue/stop", methods=["POST"])
def queue_stop():
    """Stop processing and clear queue"""
    global queue_processing, download_queue
    
    # Stop any active downloads
    try:
        active_downloads = aria2_rpc_call("aria2.tellActive", [])
        for dl in active_downloads:
            gid = dl.get("gid")
            # Check if this is a queued download
            with queue_lock:
                for item in download_queue:
                    if item.get("gid") == gid:
                        try:
                            aria2_rpc_call("aria2.remove", [gid])
                            print(f"QUEUE: Stopped download {item.get('title')}")
                        except:
                            pass
                        break
    except Exception as e:
        print(f"QUEUE: Error stopping downloads: {e}")
    
    # Clear queue
    with queue_lock:
        queue_processing = False
        download_queue = []
    
    save_queue()
    flash("Queue stopped and cleared", "ok")
    return redirect(url_for("queue_page"))


@app.route("/queue/clear", methods=["POST"])
def queue_clear():
    """Clear completed items from queue"""
    global download_queue
    
    with queue_lock:
        # Only clear completed items
        download_queue = [item for item in download_queue if not item.get("completed", False)]
    
    save_queue()
    flash("Cleared completed items from queue", "ok")
    return redirect(url_for("queue_page"))


@app.route("/aria2/config")
def aria2_config():
    """Serve AriaNg with injected configuration script"""
    aria2_config_section = cfg.get("aria2", {})
    rpc_secret = aria2_config_section.get("rpc_secret", "")
    rpc_url = aria2_config_section.get("rpc_url", "http://localhost:6800/jsonrpc")
    
    from urllib.parse import urlparse
    parsed = urlparse(rpc_url)
    aria2_host = parsed.hostname or "localhost"
    aria2_port = parsed.port or 6800
    
    # Get the Pi's IP address for RPC connection
    request_host = request.host.split(':')[0]
    if request_host in ['localhost', '127.0.0.1']:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            aria2_rpc_host = s.getsockname()[0]
            s.close()
        except:
            aria2_rpc_host = "localhost"
    else:
        aria2_rpc_host = request_host
    
    ariang_path = os.path.join("static", "ariang", "index.html")
    if not os.path.exists(ariang_path):
        return "AriaNg not found. Please run download_ariang.sh", 404
    
    try:
        with open(ariang_path, 'r', encoding='utf-8') as f:
            ariang_html = f.read()
        
        # Aggressive script: set secret in multiple ways and keep it set
        injection_script = f"""
        <script>
        (function() {{
            var secretToken = {json.dumps(rpc_secret) if rpc_secret else 'null'};
            var rpcHost = {json.dumps(aria2_rpc_host)};
            var rpcPort = {json.dumps(aria2_port)};
            
            if (!secretToken) return;
            
            function forceSetSecret() {{
                try {{
                    // Method 1: Try 'ariaNgSetting' key
                    var stored = localStorage.getItem('ariaNgSetting');
                    if (stored) {{
                        var settings = JSON.parse(stored);
                        if (!settings.rpcList) settings.rpcList = [];
                        var found = false;
                        for (var i = 0; i < settings.rpcList.length; i++) {{
                            if (settings.rpcList[i].address === rpcHost && settings.rpcList[i].port == rpcPort) {{
                                settings.rpcList[i].secret = secretToken;
                                found = true;
                                break;
                            }}
                        }}
                        if (!found) {{
                            settings.rpcList.push({{
                                alias: rpcHost + ':' + rpcPort,
                                protocol: 'http',
                                address: rpcHost,
                                port: parseInt(rpcPort),
                                path: '/jsonrpc',
                                method: 'POST',
                                secret: secretToken
                            }});
                        }}
                        localStorage.setItem('ariaNgSetting', JSON.stringify(settings));
                    }}
                    
                    // Method 2: Try 'AriaNg.Options' key (alternative format)
                    var options = localStorage.getItem('AriaNg.Options');
                    if (options) {{
                        var opts = JSON.parse(options);
                        if (!opts.rpcList) opts.rpcList = [];
                        var found2 = false;
                        for (var i = 0; i < opts.rpcList.length; i++) {{
                            if (opts.rpcList[i].address === rpcHost && opts.rpcList[i].port == rpcPort) {{
                                opts.rpcList[i].secret = secretToken;
                                found2 = true;
                                break;
                            }}
                        }}
                        if (!found2) {{
                            opts.rpcList.push({{
                                alias: rpcHost + ':' + rpcPort,
                                protocol: 'http',
                                address: rpcHost,
                                port: parseInt(rpcPort),
                                path: '/jsonrpc',
                                method: 'POST',
                                secret: secretToken
                            }});
                        }}
                        localStorage.setItem('AriaNg.Options', JSON.stringify(opts));
                    }}
                    
                    // Method 3: Directly find and set the input field, then CLICK ACTIVATE
                    var inputs = document.querySelectorAll('input[type="text"], input[type="password"]');
                    for (var i = 0; i < inputs.length; i++) {{
                        var input = inputs[i];
                        var label = input.closest('tr') ? input.closest('tr').querySelector('td:first-child') : null;
                        if (label && label.textContent && label.textContent.toLowerCase().indexOf('secret') !== -1) {{
                            if (input.value !== secretToken) {{
                                input.value = secretToken;
                                input.focus();
                                // Trigger all possible events
                                ['input', 'change', 'keyup', 'blur'].forEach(function(eventType) {{
                                    var evt = new Event(eventType, {{ bubbles: true, cancelable: true }});
                                    input.dispatchEvent(evt);
                                }});
                                // Try Angular
                                if (typeof angular !== 'undefined') {{
                                    try {{
                                        var scope = angular.element(input).scope();
                                        if (scope) {{
                                            scope.$apply();
                                        }}
                                    }} catch(e) {{
                                        // Ignore
                                    }}
                                }}
                                
                                // Find and click the Activate button after a short delay
                                setTimeout(function() {{
                                    var activateBtn = document.querySelector('button:contains("Activate"), button[ng-click*="activate"], button[ng-click*="save"]');
                                    if (!activateBtn) {{
                                        // Try finding by text content
                                        var buttons = document.querySelectorAll('button');
                                        for (var j = 0; j < buttons.length; j++) {{
                                            if (buttons[j].textContent && buttons[j].textContent.toLowerCase().indexOf('activate') !== -1) {{
                                                activateBtn = buttons[j];
                                                break;
                                            }}
                                        }}
                                    }}
                                    if (activateBtn && !activateBtn.disabled) {{
                                        activateBtn.click();
                                        console.log('Clicked Activate button');
                                    }}
                                }}, 500);
                            }}
                        }}
                    }}
                    
                    // Method 4: Use Angular service if available
                    if (typeof angular !== 'undefined') {{
                        try {{
                            var body = angular.element(document.body);
                            if (body.length > 0) {{
                                var injector = body.injector();
                                if (injector) {{
                                    var ariaNgSettingService = injector.get('ariaNgSettingService');
                                    if (ariaNgSettingService) {{
                                        var rpcs = ariaNgSettingService.getRpcList();
                                        for (var j = 0; j < rpcs.length; j++) {{
                                            if (rpcs[j].address === rpcHost && rpcs[j].port == rpcPort) {{
                                                rpcs[j].secret = secretToken;
                                                ariaNgSettingService.saveRpc(rpcs[j]);
                                                ariaNgSettingService.setCurrentRpc(rpcs[j]);
                                                break;
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }} catch(e) {{
                            // Ignore
                        }}
                    }}
                }} catch(e) {{
                    console.error('Failed to set secret:', e);
                }}
            }}
            
            // Run immediately
            forceSetSecret();
            
            // Run on load
            if (document.readyState !== 'complete') {{
                window.addEventListener('load', function() {{
                    setTimeout(forceSetSecret, 500);
                    setTimeout(forceSetSecret, 1500);
                    setTimeout(forceSetSecret, 3000);
                }});
            }} else {{
                setTimeout(forceSetSecret, 500);
                setTimeout(forceSetSecret, 1500);
                setTimeout(forceSetSecret, 3000);
            }}
            
            // Keep setting it every 2 seconds
            setInterval(forceSetSecret, 2000);
        }})();
        </script>
        """
        
        # Inject script BEFORE </body>
        if '</body>' in ariang_html:
            ariang_html = ariang_html.replace('</body>', injection_script + '</body>')
        else:
            ariang_html += injection_script
        
        # Add command API hash for RPC address
        config_hash = f"#!/settings/rpc/set/http/{aria2_rpc_host}/{aria2_port}/jsonrpc"
        # Inject hash into page load
        hash_script = f"<script>if (!window.location.hash || window.location.hash === '#') {{ window.location.hash = '{config_hash}'; }}</script>"
        ariang_html = ariang_html.replace('</head>', hash_script + '</head>', 1)
        
        # ALSO: Pre-fill the secret token in localStorage IMMEDIATELY before page loads
        # This runs before AriaNg initializes
        prefill_script = f"""
        <script>
        (function() {{
            var secretToken = {json.dumps(rpc_secret) if rpc_secret else 'null'};
            var rpcHost = {json.dumps(aria2_rpc_host)};
            var rpcPort = {json.dumps(aria2_port)};
            
            if (!secretToken) return;
            
            // Set in localStorage BEFORE AriaNg loads
            try {{
                // Try to get existing settings
                var stored = localStorage.getItem('ariaNgSetting');
                var settings = stored ? JSON.parse(stored) : {{ rpcList: [] }};
                
                if (!settings.rpcList) settings.rpcList = [];
                
                // Find or create RPC
                var rpc = null;
                for (var i = 0; i < settings.rpcList.length; i++) {{
                    if (settings.rpcList[i].address === rpcHost && settings.rpcList[i].port == rpcPort) {{
                        rpc = settings.rpcList[i];
                        break;
                    }}
                }}
                
                if (!rpc) {{
                    rpc = {{
                        alias: rpcHost + ':' + rpcPort,
                        protocol: 'http',
                        address: rpcHost,
                        port: parseInt(rpcPort),
                        path: '/jsonrpc',
                        method: 'POST',
                        secret: secretToken
                    }};
                    settings.rpcList.push(rpc);
                }} else {{
                    rpc.secret = secretToken;
                }}
                
                // Save immediately
                localStorage.setItem('ariaNgSetting', JSON.stringify(settings));
                console.log('Pre-filled secret token in localStorage');
            }} catch(e) {{
                console.error('Pre-fill failed:', e);
            }}
        }})();
        </script>
        """
        # Inject at the very start of <head> so it runs first
        ariang_html = ariang_html.replace('<head>', '<head>' + prefill_script, 1)
        
        from flask import Response
        return Response(ariang_html, mimetype='text/html')
        
    except Exception as e:
        print(f"ERROR: Failed to inject AriaNg configuration: {e}")
        import traceback
        traceback.print_exc()
        return f"Error loading AriaNg: {e}", 500


@app.route("/aria2")
def aria2_ui():
    """Serve AriaNg web UI locally (HTTP) so HTTP protocol option is enabled"""
    aria2_config = cfg.get("aria2", {})
    rpc_secret = aria2_config.get("rpc_secret", "")
    rpc_url = aria2_config.get("rpc_url", "http://localhost:6800/jsonrpc")
    
    from urllib.parse import urlparse
    parsed = urlparse(rpc_url)
    aria2_host = parsed.hostname or "localhost"
    aria2_port = parsed.port or 6800
    
    # Get the Pi's IP address for RPC connection
    request_host = request.host.split(':')[0]
    if request_host in ['localhost', '127.0.0.1']:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            aria2_rpc_host = s.getsockname()[0]
            s.close()
        except:
            aria2_rpc_host = "localhost"
    else:
        aria2_rpc_host = request_host
    
    # Check if AriaNg is available locally
    ariang_path = os.path.join("static", "ariang", "index.html")
    if os.path.exists(ariang_path):
        # Test if Aria2 is accessible
        aria2_accessible = False
        try:
            test_result = aria2_rpc_call("aria2.getVersion")
            aria2_accessible = True
        except Exception as e:
            print(f"DEBUG: Aria2 not accessible: {e}")
        
        # Serve wrapper HTML with iframe pointing to /aria2/config
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>AriaNg - Rom-Pi</title>
            <meta charset="utf-8">
            <style>
                body {{ margin: 0; padding: 0; font-family: Arial, sans-serif; background: #1a1a2e; color: #fff; }}
                .header {{
                    background: rgba(255,255,255,0.1);
                    padding: 15px 20px;
                    border-bottom: 1px solid rgba(255,255,255,0.1);
                }}
                .header h2 {{ margin: 0; display: inline-block; }}
                .header a {{ color: #4a9eff; text-decoration: none; margin-left: 20px; }}
                .header a:hover {{ text-decoration: underline; }}
                .status {{
                    display: inline-block;
                    margin-left: 20px;
                    padding: 5px 10px;
                    border-radius: 4px;
                    font-size: 12px;
                    background: {'rgba(0,255,0,0.2)' if aria2_accessible else 'rgba(255,0,0,0.2)'};
                    color: {'#0f0' if aria2_accessible else '#f00'};
                }}
                .instructions {{
                    background: rgba(255,255,255,0.05);
                    padding: 10px 20px;
                    font-size: 12px;
                    border-bottom: 1px solid rgba(255,255,255,0.1);
                }}
                .instructions strong {{ color: #4a9eff; }}
                iframe {{ width: 100%; height: calc(100vh - 100px); border: none; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h2>📥 AriaNg - Download Manager</h2>
                <a href="/">← Back to Rom-Pi</a>
                <span class="status">Aria2: {'✓ Connected' if aria2_accessible else '✗ Not Connected - Check if service is running'}</span>
            </div>
            <div class="instructions">
                <br><br>
                <strong>Code:</strong> {rpc_secret if rpc_secret else 'Not set'}
            </div>
            <iframe src="/aria2/config" title="AriaNg"></iframe>
        </body>
        </html>
        """
    else:
        # AriaNg not downloaded yet - show instructions
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>AriaNg Setup - Rom-Pi</title>
            <meta charset="utf-8">
            <style>
                body {{
                    margin: 0;
                    padding: 40px;
                    font-family: Arial, sans-serif;
                    background: #1a1a2e;
                    color: #fff;
                }}
                .container {{
                    max-width: 800px;
                    margin: 0 auto;
                    background: rgba(255,255,255,0.1);
                    padding: 30px;
                    border-radius: 8px;
                }}
                h1 {{ color: #4a9eff; }}
                code {{
                    background: rgba(0,0,0,0.3);
                    padding: 2px 6px;
                    border-radius: 4px;
                    font-family: monospace;
                }}
                pre {{
                    background: rgba(0,0,0,0.3);
                    padding: 15px;
                    border-radius: 4px;
                    overflow-x: auto;
                }}
                a {{
                    color: #4a9eff;
                    text-decoration: none;
                }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>📥 AriaNg Setup Required</h1>
                <p>AriaNg needs to be downloaded and served locally so the HTTP protocol option is enabled.</p>
                <p><strong>Run this command on your Raspberry Pi:</strong></p>
                <pre>cd /opt/rompi
chmod +x download_ariang.sh
./download_ariang.sh</pre>
                <p>Or manually:</p>
                <pre>cd /opt/rompi
mkdir -p static/ariang
cd static/ariang
wget https://github.com/mayswind/AriaNg/releases/latest/download/AriaNg-1.3.7-AllInOne.zip
unzip AriaNg-1.3.7-AllInOne.zip
rm AriaNg-1.3.7-AllInOne.zip</pre>
                <p>After downloading, <a href="/aria2">refresh this page</a>.</p>
                <p><a href="/">← Back to Rom-Pi</a></p>
            </div>
        </body>
        </html>
        """


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
