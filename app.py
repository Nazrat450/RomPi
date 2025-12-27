from __future__ import annotations

from flask import Flask, render_template, request, redirect, url_for, flash
import yaml
import re
import requests
import os
import threading
import urllib3
from pathlib import Path

# Disable SSL warnings for vimm.net (certificate verification disabled)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from jackett import (
    JackettError,
    search_all_indexers,
    search_one_indexer,
    list_indexers,
    download_torrent_bytes,
)
from qbittorrent import Qbit
from vimm import search_vimm, VimmError

app = Flask(__name__)
app.secret_key = "Eggs?"

PER_PAGE = 50

# --- Load config safely ---
with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)


def get_qbit() -> Qbit:
    return Qbit(
        cfg["qbittorrent"]["base_url"],
        cfg["qbittorrent"]["username"],
        cfg["qbittorrent"]["password"],
    )


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


def run_search(query: str, selected_indexer: str):
    results = []
    
    # Get Jackett results (existing logic)
    try:
        if selected_indexer == "all":
            results = search_all_indexers(
                query=query,
                base_url=cfg["jackett"]["base_url"],
                api_key=cfg["jackett"]["api_key"],
            )
        else:
            results = search_one_indexer(
                query=query,
                base_url=cfg["jackett"]["base_url"],
                api_key=cfg["jackett"]["api_key"],
                indexer_id=selected_indexer,
            )
    except JackettError as e:
        # Show error but continue to try vimm.net
        flash(str(e), "error")
        results = []
    except Exception as e:
        # Catch any other exceptions from Jackett (timeouts, connection errors, etc.)
        flash(f"Jackett error: {e}", "error")
        results = []
    
    # Add vimm.net results - always try this even if Jackett failed
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
    # dropdown options (always try to show them)
    # If this fails, we can still search with "all indexers" so don't show scary errors
    try:
        indexers = list_indexers(
            base_url=cfg["jackett"]["base_url"],
            api_key=cfg["jackett"]["api_key"],
        )
        indexers = sorted(indexers, key=lambda x: (x.get("Title") or "").lower())
    except JackettError as e:
        # Only show error if it's not the common login redirect issue
        # The login redirect error is annoying but doesn't block functionality
        error_msg = str(e)
        if "login" not in error_msg.lower() and "redirect" not in error_msg.lower():
            flash(str(e), "error")
        # Silently fail for login redirects - user can still use "All indexers"
        indexers = []
    except Exception:
        indexers = []

    # current state (defaults)
    results = []
    query = ""
    selected_indexer = "all"
    mode = "games"

    # page (GET param only)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        selected_indexer = (request.form.get("indexer", "all") or "all").strip()

        only_books = request.form.get("only_books") == "on"
        only_direct = request.form.get("only_direct") == "on"
        
        if only_direct:
            mode = "direct"
        elif only_books:
            mode = "books"
        else:
            mode = "games"

        if query:
            return redirect(url_for("index", page=1, q=query, ix=selected_indexer, mode=mode))

    # GET: perform the search/paginate
    query = request.args.get("q", "").strip() or query
    selected_indexer = (request.args.get("ix", selected_indexer) or "all").strip()
    mode = (request.args.get("mode", mode) or "games").strip()
    if mode not in ("games", "books", "direct"):
        mode = "games"

    if query:
        results = run_search(query, selected_indexer)
        results = decorate_results(results)  # Decorate first to set IsDirectDownload
        results = filter_by_mode(results, mode)  # Then filter based on mode

    page_items, total_items, page, total_pages = paginate(results, page, PER_PAGE)

    return render_template(
        "index.html",
        results=page_items,
        query=query,
        indexers=indexers,
        selected_indexer=selected_indexer,
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

        # 2) Torrent URL: Try direct URL first (qBittorrent can download .torrent files directly)
        # Only use Jackett proxy as a last resort since it's blocked by login
        if torrent_url.startswith("http://") or torrent_url.startswith("https://"):
            # First, try direct URL - qBittorrent can handle most torrent URLs directly
            try:
                q.add_url(torrent_url)
                flash(f"Sent to qBittorrent (direct URL): {title}", "ok")
                return redirect(url_for("index"))
            except (requests.exceptions.RequestException, RuntimeError) as direct_error:
                # Direct URL failed, try Jackett proxy as fallback
                # This handles cases where the tracker requires authentication/cookies
                try:
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
    
    # Try to get filename from URL or use title
    try:
        # Make HEAD request to get filename from Content-Disposition header
        head_response = requests.head(download_url, allow_redirects=True, timeout=10)
        content_disp = head_response.headers.get("Content-Disposition", "")
        if "filename=" in content_disp:
            # Extract filename from header
            filename = content_disp.split("filename=")[1].strip('"\'')
        else:
            # Fallback: use title + extension from URL
            ext = os.path.splitext(download_url.split("?")[0])[1] or ".zip"
            filename = f"{safe_title}{ext}"
    except:
        # If HEAD fails, use title with .zip extension
        filename = f"{safe_title}.zip"
    
    file_path = os.path.join(download_dir, filename)
    
    # Check if file already exists
    if os.path.exists(file_path):
        flash(f"File already exists: {filename}", "error")
        return redirect(url_for("index"))
    
    # Download file in background thread
    # Capture variables for the closure
    download_url_local = download_url
    file_path_local = file_path
    filename_local = filename
    game_page_url_local = game_page_url
    
    def download_file():
        final_file_path = file_path_local  # Will be updated if we get filename from Content-Disposition
        try:
            print(f"DOWNLOAD: Starting download: {download_url_local} -> {file_path_local}")
            
            # Extract mediaId from URL
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(download_url_local)
            params = parse_qs(parsed.query)
            media_id = params.get('mediaId', [None])[0]
            
            if not media_id:
                raise ValueError(f"Could not extract mediaId from URL: {download_url_local}")
            
            print(f"DOWNLOAD: Extracted mediaId: {media_id}")
            
            # vimm.net requires POST request with proper headers and form data
            # Use the same session setup as vimm.py for consistency
            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            })
            
            # First, visit the specific game page to get cookies and set referer (simulate browser behavior)
            if game_page_url_local and game_page_url_local.startswith("http"):
                print(f"DOWNLOAD: Visiting game page to get cookies: {game_page_url_local}")
                page_response = session.get(game_page_url_local, verify=False, timeout=10)
                print(f"DOWNLOAD: Game page response status: {page_response.status_code}")
                referer = game_page_url_local
            else:
                # Fallback: visit vault homepage
                fallback_url = "https://vimm.net/vault/"
                print(f"DOWNLOAD: Visiting vault homepage to get cookies: {fallback_url}")
                session.get(fallback_url, verify=False, timeout=10)
                referer = fallback_url
            
            # Add a small delay to simulate human behavior (vimm.net might check for this)
            import time
            time.sleep(1)
            
            # Set referer header for the download request
            session.headers.update({"Referer": referer})
            
            # vimm.net uses GET request (JavaScript changes form method to GET)
            # The request must include proper headers and cookies from visiting the game page
            download_endpoint = "https://dl3.vimm.net/"
            print(f"DOWNLOAD: Making GET request to {download_endpoint} with mediaId={media_id}")
            
            # Add Sec-Fetch-* headers to mimic browser behavior
            session.headers.update({
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-site",
                "Sec-Fetch-User": "?1",
            })
            
            # Make GET request with mediaId as query parameter
            response = session.get(
                download_endpoint,
                params={"mediaId": media_id},
                stream=True,
                timeout=300,
                verify=False,
                allow_redirects=True
            )
            
            print(f"DOWNLOAD: Response status: {response.status_code}")
            print(f"DOWNLOAD: Response headers: {dict(response.headers)}")
            
            # Check response before raising
            if response.status_code != 200:
                # Log the response body for debugging
                response_text = response.text[:1000]  # First 1000 chars
                print(f"DOWNLOAD: Response body (first 1000 chars): {response_text}")
                response.raise_for_status()
            
            # Check if we got redirected to an error page
            if "shall not pass" in response.text.lower() or "acting funny" in response.text.lower():
                raise RuntimeError("vimm.net blocked the download request (bot detection). Try again later.")
            
            # Extract filename from Content-Disposition header if available
            content_disp = response.headers.get('Content-Disposition', '')
            if 'filename=' in content_disp:
                # Extract filename from Content-Disposition: attachment; filename="..."
                import re
                filename_match = re.search(r'filename[^;=\n]*=(([\'"]).*?\2|[^;\n]*)', content_disp)
                if filename_match:
                    extracted_filename = filename_match.group(1).strip('"\'')
                    if extracted_filename:
                        # Update file path with the actual filename from server
                        # Use the directory from the original file_path_local
                        final_file_path = os.path.join(os.path.dirname(file_path_local), extracted_filename)
                        print(f"DOWNLOAD: Using filename from Content-Disposition: {extracted_filename}")
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            print(f"DOWNLOAD: Download started, total size: {total_size} bytes ({human_size(total_size)})")
            print(f"DOWNLOAD: Response status: {response.status_code}, Content-Type: {response.headers.get('Content-Type', 'unknown')}")
            
            with open(final_file_path, 'wb') as f:
                last_logged = 0
                log_interval = max(1024 * 1024, total_size // 20)  # Log every 1 MB or every 5% of total, whichever is smaller
                
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Log progress more frequently
                        if total_size > 0:
                            if downloaded - last_logged >= log_interval or downloaded >= total_size:
                                percent = (downloaded / total_size) * 100
                                print(f"DOWNLOAD: Progress: {percent:.1f}% ({human_size(downloaded)}/{human_size(total_size)})")
                                last_logged = downloaded
                        else:
                            # If we don't know total size, log every 1 MB
                            if downloaded - last_logged >= (1024 * 1024):
                                print(f"DOWNLOAD: Progress: {human_size(downloaded)} downloaded (total size unknown)")
                                last_logged = downloaded
            
            file_size = os.path.getsize(final_file_path)
            print(f"DOWNLOAD: ✅ Download completed successfully: {final_file_path} ({human_size(file_size)})")
        except requests.exceptions.RequestException as e:
            print(f"DOWNLOAD: ❌ Download failed (RequestException): {e}")
            import traceback
            print(f"DOWNLOAD: Traceback:\n{traceback.format_exc()}")
            # Clean up partial file
            if 'final_file_path' in locals() and os.path.exists(final_file_path):
                os.remove(final_file_path)
                print(f"DOWNLOAD: Cleaned up partial file: {final_file_path}")
            elif os.path.exists(file_path_local):
                os.remove(file_path_local)
                print(f"DOWNLOAD: Cleaned up partial file: {file_path_local}")
        except Exception as e:
            import traceback
            print(f"DOWNLOAD: ❌ Download error: {e}")
            print(f"DOWNLOAD: Traceback:\n{traceback.format_exc()}")
            if 'final_file_path' in locals() and os.path.exists(final_file_path):
                os.remove(final_file_path)
                print(f"DOWNLOAD: Cleaned up partial file: {final_file_path}")
            elif os.path.exists(file_path_local):
                os.remove(file_path_local)
                print(f"DOWNLOAD: Cleaned up partial file: {file_path_local}")
    
    # Start download in background
    thread = threading.Thread(target=download_file, daemon=True)
    thread.start()
    
    flash(f"Starting download: {filename}...", "ok")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
