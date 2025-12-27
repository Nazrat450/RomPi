# vimm.py
from __future__ import annotations

import requests
from bs4 import BeautifulSoup
import re
import urllib3
from urllib.parse import urljoin, urlparse, parse_qs, quote

# Disable SSL warnings for vimm.net (certificate verification disabled)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class VimmError(RuntimeError):
    pass


def _session() -> requests.Session:
    """Create a requests session with proper headers for vimm.net"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    # Disable SSL verification to avoid certificate issues on Raspberry Pi
    s.verify = False
    return s


def _extract_media_id(download_url: str) -> str | None:
    """Extract mediaId from vimm.net download URL"""
    if not download_url:
        return None
    
    # Handle both direct URLs and relative paths
    if "mediaId=" in download_url:
        parsed = parse_qs(urlparse(download_url).query)
        media_id = parsed.get("mediaId", [None])[0]
        return media_id
    
    # Check if it's already a mediaId
    if download_url.isdigit():
        return download_url
    
    return None


def _parse_size(size_str: str) -> int:
    """Parse size string (e.g., '746 KB', '1.2 MB') to bytes"""
    if not size_str:
        return 0
    
    size_str = size_str.strip().upper()
    
    # Extract number and unit
    match = re.match(r"([\d.]+)\s*([KMGT]?B?)", size_str)
    if not match:
        return 0
    
    number = float(match.group(1))
    unit = match.group(2) or "B"
    
    multipliers = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 * 1024,
        "GB": 1024 * 1024 * 1024,
        "TB": 1024 * 1024 * 1024 * 1024,
    }
    
    return int(number * multipliers.get(unit, 1))


def search_vimm(query: str) -> list[dict]:
    """
    Search vimm.net vault and return results in Jackett-compatible format.
    
    Args:
        query: Search query string
        
    Returns:
        List of dicts with keys: Title, Link, Size, Tracker, Seeders, CategoryDesc
    """
    if not query or not query.strip():
        return []
    
    results = []
    base_url = "https://vimm.net"
    search_url = f"{base_url}/vault/?p=list&q={quote(query)}"
    game_links = []  # Track outside try block for final check
    
    try:
        s = _session()
        response = s.get(search_url, timeout=30, allow_redirects=True)
        response.raise_for_status()
        
        # Debug: Check if we got a valid response
        if len(response.text) < 100:
            # Very short response might be an error page
            raise VimmError(f"Vimm.net returned very short response ({len(response.text)} chars) - might be an error page")
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Check if we got redirected to a login page or error page
        if "login" in response.url.lower() or "error" in response.url.lower():
            raise VimmError(f"Vimm.net redirected to: {response.url}")
        
        # vimm.net search results with ?p=list format: results are in a table
        # HTML structure: <table><tr><td>Platform</td><td><a href="/vault/64996">Title</a></td>...</tr></table>
        seen_urls = set()
        
        print(f"DEBUG: Vimm.net - Starting to parse HTML, response length: {len(response.text)}")
        
        # Find the main results table (has class "hovertable" or "striped")
        results_table = soup.select_one('table.hovertable, table.striped, table.rounded')
        if not results_table:
            # Fallback: find any table
            results_table = soup.select_one('table')
            print(f"DEBUG: Vimm.net - No hovertable/striped table found, using first table")
        
        if results_table:
            print(f"DEBUG: Vimm.net - Found results table with classes: {results_table.get('class', [])}")
            # Find all table rows in the results table
            table_rows = results_table.select('tr')
            print(f"DEBUG: Vimm.net - Found {len(table_rows)} table rows")
            
            for row in table_rows:
                # Skip header rows (contain <th> or are in <caption>)
                if row.select_one('th') or row.find_parent('caption'):
                    continue
                
                # Look for <a> tags with /vault/ href in this row
                # The structure is: <td><a href="/vault/64996">Game Title</a></td>
                row_links = row.select('a[href*="/vault/"]')
                
                for link in row_links:
                    href = link.get('href', '').strip()
                    link_text = link.get_text(strip=True)
                    
                    # Filter: must be /vault/ followed by numeric ID, and have link text
                    if '/vault/' in href and href != '/vault/' and '/vault/?' not in href and link_text:
                        # Extract game ID from href like "/vault/64996" or " /vault/64996" (note space)
                        # Normalize href (remove leading space if present)
                        href = href.lstrip()
                        parts = href.split('/vault/')
                        if len(parts) > 1:
                            game_id = parts[1].split('?')[0].split('#')[0].strip()
                            # vimm.net uses numeric IDs (e.g., 64996)
                            if game_id and game_id.strip() and game_id.isdigit():
                                full_url = urljoin(base_url, href)
                                if full_url not in seen_urls:
                                    seen_urls.add(full_url)
                                    game_links.append((full_url, link))
        
        # Fallback: if no table found, try direct link selection
        if not game_links:
            all_links = soup.select('a[href*="/vault/"]')
            for link in all_links:
                href = link.get('href', '').strip().lstrip()
                link_text = link.get_text(strip=True)
                if '/vault/' in href and href != '/vault/' and '/vault/?' not in href and link_text:
                    parts = href.split('/vault/')
                    if len(parts) > 1:
                        game_id = parts[1].split('?')[0].split('#')[0].strip()
                        if game_id and game_id.strip() and game_id.isdigit():
                            full_url = urljoin(base_url, href)
                            if full_url not in seen_urls:
                                seen_urls.add(full_url)
                                game_links.append((full_url, link))
        
        # Also try direct link selection as fallback (in case table structure is different)
        if not game_links:
            all_links = soup.select('a[href*="/vault/"]')
            seen_urls = set(url for url, _ in game_links)
            for link in all_links:
                href = link.get('href', '')
                if '/vault/' in href and href != '/vault/' and '/vault/?' not in href:
                    parts = href.split('/vault/')
                    if len(parts) > 1:
                        game_id = parts[1].split('?')[0].split('#')[0].strip()
                        # Accept any game ID format
                        if game_id and game_id.strip() and len(game_id) > 1:
                            full_url = urljoin(base_url, href)
                            if full_url not in seen_urls:
                                seen_urls.add(full_url)
                                game_links.append((full_url, link))
        
        # If no game links found, try a more permissive search
        if not game_links:
            # Try finding any links that look like game pages
            all_links = soup.select('a')
            for link in all_links:
                href = link.get('href', '')
                text = link.get_text(strip=True)
                # Look for links that might be games (have some text and go to /vault/)
                if '/vault/' in href and text and len(text) > 2:
                    parts = href.split('/vault/')
                    if len(parts) > 1:
                        game_id = parts[1].split('?')[0].split('#')[0].strip()
                        # Accept any non-empty game ID
                        if game_id and game_id.strip():
                            full_url = urljoin(base_url, href)
                            if full_url not in [url for url, _ in game_links]:
                                game_links.append((full_url, link))
        
        # If still no game links found, raise error with detailed debug info
        if not game_links:
            all_links_count = len(soup.select('a'))
            vault_links_count = len(soup.select('a[href*="/vault/"]'))
            table_rows_count = len(soup.select('table tr'))
            
            # Get detailed sample of /vault/ links for debugging
            sample_links = []
            for i, link in enumerate(soup.select('a[href*="/vault/"]')[:10]):
                href = link.get('href', '').strip()
                text = link.get_text(strip=True)
                # Try to extract game ID
                game_id = ""
                if '/vault/' in href:
                    parts = href.split('/vault/')
                    if len(parts) > 1:
                        game_id = parts[1].split('?')[0].split('#')[0].strip()
                
                # Check why it was filtered
                reason = []
                if not text:
                    reason.append("no text")
                if href == '/vault/' or '/vault/?' in href:
                    reason.append("invalid href")
                if game_id and not game_id.isdigit():
                    reason.append(f"non-numeric ID: '{game_id}'")
                if not game_id:
                    reason.append("no ID")
                
                reason_str = ", ".join(reason) if reason else "OK"
                sample_links.append(f"  {i+1}. '{text[:40]}' -> href='{href}' ID='{game_id}' ({reason_str})")
            
            sample_text = "\n".join(sample_links) if sample_links else "None found"
            
            # Check table structure
            tables = soup.select('table')
            table_info = []
            for i, table in enumerate(tables[:3]):
                classes = table.get('class', [])
                rows = len(table.select('tr'))
                table_info.append(f"  Table {i+1}: classes={classes}, {rows} rows")
            
            error_msg = f"No game links found after filtering.\n\nStats:\n  Total links: {all_links_count}\n  /vault/ links: {vault_links_count}\n  Table rows: {table_rows_count}\n\nTables found:\n" + "\n".join(table_info) + f"\n\nSample /vault/ links (showing why filtered):\n{sample_text}\n\nSearch URL: {search_url}"
            print(f"DEBUG: Vimm.net ERROR: {error_msg}")
            raise VimmError(error_msg)
        
        print(f"DEBUG: Vimm.net - Found {len(game_links)} game links, processing first 20")
        # Limit to first 20 results to avoid too many requests
        for idx, (game_url, link_elem) in enumerate(game_links[:20], 1):
            print(f"DEBUG: Vimm.net - Processing game {idx}/{min(20, len(game_links))}: {game_url}")
            try:
                # Get title from the link text
                title = link_elem.get_text(strip=True)
                if not title or len(title) < 2:
                    title = ""
                
                # Visit the game page to get download link and details
                try:
                    game_page = s.get(game_url, timeout=15)
                    game_page.raise_for_status()
                    game_soup = BeautifulSoup(game_page.text, 'html.parser')
                    
                    # Extract title from game page if we don't have it
                    if not title:
                        title_elem = game_soup.select_one('h1, .title, [class*="title"]')
                        if title_elem:
                            title = title_elem.get_text(strip=True)
                    
                    print(f"DEBUG: Vimm.net - Game {idx}: title='{title[:50] if title else 'NONE'}'")
                    
                    # Look for download link - vimm.net uses a form with hidden mediaId input
                    download_link = None
                    media_id = None
                    
                    # Method 1: Look for the download form and extract mediaId from hidden input
                    dl_form = game_soup.select_one('form#dl_form, form[action*="dl3.vimm.net"], form[action*="dl.vimm.net"]')
                    if dl_form:
                        media_input = dl_form.select_one('input[name="mediaId"]')
                        if media_input:
                            media_id = media_input.get('value', '').strip()
                            if media_id:
                                download_link = f"https://dl3.vimm.net/?mediaId={media_id}"
                                print(f"DEBUG: Vimm.net - Game {idx}: Found mediaId from form input: {media_id}")
                    
                    # Method 2: Extract from JavaScript media array (fallback)
                    if not download_link:
                        scripts = game_soup.find_all('script')
                        for script in scripts:
                            if script.string and 'mediaId' in script.string:
                                # Look for mediaId in hidden input value
                                match = re.search(r'name=["\']mediaId["\'][^>]*value=["\'](\d+)', script.string)
                                if match:
                                    media_id = match.group(1)
                                    download_link = f"https://dl3.vimm.net/?mediaId={media_id}"
                                    print(f"DEBUG: Vimm.net - Game {idx}: Found mediaId from script input: {media_id}")
                                    break
                                
                                # Also try to extract from JavaScript media array
                                # Look for pattern like: const media=[{"ID":10981,...}]
                                media_array_match = re.search(r'const\s+media\s*=\s*\[.*?\{[^}]*"ID"\s*:\s*(\d+)', script.string, re.DOTALL)
                                if media_array_match:
                                    # Get the first ID (usually the default/selected version)
                                    media_id = media_array_match.group(1)
                                    download_link = f"https://dl3.vimm.net/?mediaId={media_id}"
                                    print(f"DEBUG: Vimm.net - Game {idx}: Found mediaId from media array: {media_id}")
                                    break
                    
                    # Method 3: Search page text directly for mediaId patterns
                    if not download_link:
                        page_text = game_page.text
                        # Look for hidden input pattern
                        hidden_input_match = re.search(r'<input[^>]*name=["\']mediaId["\'][^>]*value=["\'](\d+)', page_text)
                        if hidden_input_match:
                            media_id = hidden_input_match.group(1)
                            download_link = f"https://dl3.vimm.net/?mediaId={media_id}"
                            print(f"DEBUG: Vimm.net - Game {idx}: Found mediaId from page text (hidden input): {media_id}")
                        else:
                            # Look for any mediaId= pattern in URLs
                            url_match = re.search(r'mediaId["\']?\s*[:=]\s*["\']?(\d+)', page_text)
                            if url_match:
                                media_id = url_match.group(1)
                                download_link = f"https://dl3.vimm.net/?mediaId={media_id}"
                                print(f"DEBUG: Vimm.net - Game {idx}: Found mediaId from page text (URL pattern): {media_id}")
                    
                    if not download_link:
                        print(f"DEBUG: Vimm.net - Game {idx}: NO download link found!")
                        # Debug: show what forms we found
                        all_forms = game_soup.find_all('form')
                        print(f"DEBUG: Vimm.net - Game {idx}: Found {len(all_forms)} form(s) on page")
                        for f_idx, f in enumerate(all_forms):
                            form_action = f.get('action', '')
                            form_id = f.get('id', '')
                            print(f"DEBUG: Vimm.net - Game {idx}: Form {f_idx}: id='{form_id}', action='{form_action[:100]}'")
                    
                    # Extract platform/system from game page
                    platform = ""
                    
                    # Map common platform codes to readable names
                    platform_map = {
                        'GB': 'Game Boy', 'GBC': 'Game Boy Color', 'GBA': 'Game Boy Advance',
                        'DS': 'Nintendo DS', '3DS': 'Nintendo 3DS',
                        'NES': 'Nintendo', 'SNES': 'Super Nintendo', 'N64': 'Nintendo 64',
                        'GameCube': 'GameCube', 'Wii': 'Wii', 'WiiWare': 'WiiWare',
                        'PS1': 'PlayStation', 'PS2': 'PlayStation 2', 'PS3': 'PlayStation 3',
                        'PSP': 'PS Portable', 'Vita': 'PS Vita',
                        'Genesis': 'Genesis', 'SMS': 'Master System', 'SegaCD': 'Sega CD',
                        'Saturn': 'Saturn', 'Dreamcast': 'Dreamcast',
                        'Xbox': 'Xbox', 'Xbox360': 'Xbox 360', 'X360-D': 'Xbox 360 (Digital)',
                        'Atari2600': 'Atari 2600', 'Atari5200': 'Atari 5200', 'Atari7800': 'Atari 7800',
                        'TG16': 'TurboGrafx-16', 'TGCD': 'TurboGrafx-CD',
                    }
                    
                    # Method 1: Extract from URL path (e.g., /vault/GB/3454 -> GB, /vault/DS/19482 -> DS)
                    if '/vault/' in game_url:
                        url_parts = game_url.split('/vault/')
                        if len(url_parts) > 1:
                            path_after_vault = url_parts[1].split('/')[0]
                            if path_after_vault in platform_map:
                                platform = platform_map[path_after_vault]
                    
                    # Method 2: Extract from page title (e.g., "The Vault: Pokemon: Red Version (GB)")
                    if not platform:
                        title_tag = game_soup.find('title')
                        if title_tag:
                            title_text = title_tag.get_text()
                            # Look for platform in parentheses like "(GB)", "(DS)", etc.
                            platform_match = re.search(r'\(([A-Z0-9]+)\)', title_text)
                            if platform_match:
                                platform_code = platform_match.group(1)
                                if platform_code in platform_map:
                                    platform = platform_map[platform_code]
                    
                    # Method 3: Extract from section title
                    if not platform:
                        section_title = game_soup.select_one('.sectionTitle, h2 .sectionTitle')
                        if section_title:
                            platform = section_title.get_text(strip=True)
                    
                    # Method 4: Fallback to selectors
                    if not platform:
                        platform_selectors = [
                            '.platform', '.system', '.console',
                            '[class*="platform"]', '[class*="system"]',
                        ]
                        for selector in platform_selectors:
                            platform_elem = game_soup.select_one(selector)
                            if platform_elem:
                                platform = platform_elem.get_text(strip=True)
                                break
                    
                    # Extract file size from multiple sources
                    size = 0
                    
                    # Get all scripts once for both size and filename extraction
                    scripts = game_soup.find_all('script')
                    
                    # Method 1: Extract from JavaScript media array (most reliable)
                    for script in scripts:
                        if script.string and 'ZippedText' in script.string:
                            # Look for ZippedText in the media array
                            zipped_match = re.search(r'"ZippedText"\s*:\s*"([^"]+)"', script.string)
                            if zipped_match:
                                size_str = zipped_match.group(1)
                                size = _parse_size(size_str)
                                if size > 0:
                                    break
                    
                    # Method 2: Extract from HTML element with id="dl_size"
                    if size == 0:
                        size_elem = game_soup.select_one('#dl_size')
                        if size_elem:
                            size_str = size_elem.get_text(strip=True)
                            size = _parse_size(size_str)
                    
                    # Method 3: Fallback to other size selectors
                    if size == 0:
                        size_selectors = [
                            '.size', '[class*="size"]',
                        ]
                        for selector in size_selectors:
                            size_elem = game_soup.select_one(selector)
                            if size_elem:
                                size_str = size_elem.get_text(strip=True)
                                size = _parse_size(size_str)
                                if size > 0:
                                    break
                    
                    # Extract filename from JavaScript media array (base64 encoded GoodTitle)
                    filename = ""
                    for script in scripts:
                        if script.string and 'GoodTitle' in script.string:
                            # Look for GoodTitle in the media array
                            goodtitle_match = re.search(r'"GoodTitle"\s*:\s*"([^"]+)"', script.string)
                            if goodtitle_match:
                                try:
                                    import base64
                                    encoded_title = goodtitle_match.group(1)
                                    decoded_title = base64.b64decode(encoded_title).decode('utf-8')
                                    filename = decoded_title
                                    print(f"DEBUG: Vimm.net - Game {idx}: Extracted filename: {filename}")
                                    break
                                except:
                                    pass
                    
                    # Only add if we have a title and download link
                    if title and download_link:
                        # Ensure download link has mediaId
                        media_id = _extract_media_id(download_link)
                        if not media_id and download_link:
                            # Try to construct from URL if it's a game page
                            print(f"DEBUG: Vimm.net - Game {idx}: download_link has no mediaId, skipping")
                            continue
                        
                        if media_id:
                            download_link = f"https://dl3.vimm.net/?mediaId={media_id}"
                        
                        results.append({
                            "Title": title,
                            "Link": download_link,
                            "TorrentLink": download_link,
                            "Size": size,
                            "Tracker": "Vimm.net",
                            "Seeders": 0,
                            "CategoryDesc": platform or "ROM",
                            "MagnetUri": "",
                            "GamePageUrl": game_url,  # Store game page URL for referer
                            "Filename": filename,  # Store extracted filename
                        })
                        print(f"DEBUG: Vimm.net - Game {idx}: Added result for '{title[:50]}'")
                    else:
                        print(f"DEBUG: Vimm.net - Game {idx}: Skipped - title={bool(title)}, download_link={bool(download_link)}")
                
                except requests.exceptions.RequestException as e:
                    # Skip this game page if it fails to load
                    print(f"DEBUG: Vimm.net - Game {idx}: RequestException: {e}")
                    continue
                except Exception as e:
                    # Skip this game if parsing fails, but log the error for debugging
                    import traceback
                    print(f"DEBUG: Vimm.net - Game {idx}: Exception during parsing: {e}")
                    print(f"DEBUG: Vimm.net - Game {idx}: Traceback:\n{traceback.format_exc()}")
                    continue
            
            except Exception:
                # Skip this link if anything fails
                continue
        
        # If we got no results, try a simpler approach
        if not results:
            # Fallback 1: Look for any table rows that might contain game info
            table_rows = soup.select('table tbody tr, table tr')
            for row in table_rows[:30]:  # Limit to first 30 rows
                try:
                    # Try to find a link in this row
                    row_link = row.select_one('a[href*="/vault/"]')
                    if row_link:
                        href = row_link.get('href', '')
                        title = row_link.get_text(strip=True)
                        if title and len(title) > 3 and '/vault/' in href:
                            # Try to visit this game page
                            game_url = urljoin(base_url, href)
                            try:
                                game_page = s.get(game_url, timeout=10)
                                game_soup = BeautifulSoup(game_page.text, 'html.parser')
                                
                                # Look for download button - try multiple patterns
                                dl_link = None
                                for selector in ['a[href*="mediaId"]', 'a[href*="dl"]', 'a:contains("Download")']:
                                    try:
                                        elem = game_soup.select_one(selector)
                                        if elem and elem.get('href'):
                                            dl_link = elem.get('href')
                                            break
                                    except:
                                        continue
                                
                                    # Also check page content for mediaId
                                    if not dl_link:
                                        page_text = game_page.text
                                        media_match = re.search(r'mediaId["\']?\s*[:=]\s*["\']?(\d+)', page_text)
                                        if media_match:
                                            media_id = media_match.group(1)
                                            dl_link = f"https://dl3.vimm.net/?mediaId={media_id}"
                                
                                if dl_link:
                                    media_id = _extract_media_id(dl_link)
                                    if media_id:
                                        download_url = f"https://dl3.vimm.net/?mediaId={media_id}"
                                        results.append({
                                            "Title": title,
                                            "Link": download_url,
                                            "TorrentLink": download_url,
                                            "Size": 0,
                                            "Tracker": "Vimm.net",
                                            "Seeders": 0,
                                            "CategoryDesc": "ROM",
                                            "MagnetUri": "",
                                        })
                            except:
                                continue
                except:
                    continue
            
            # Fallback 2: Look for any links with mediaId directly on search page
            if not results:
                all_dl_links = soup.select('a[href*="mediaId"], a[href*="dl3"]')
                for link in all_dl_links[:10]:  # Limit to first 10
                    href = link.get('href', '')
                    title_text = link.get_text(strip=True) or link.get('title', '')
                    if 'mediaId=' in href and title_text:
                        media_id = _extract_media_id(href)
                        if media_id:
                            download_url = f"https://dl3.vimm.net/?mediaId={media_id}"
                            results.append({
                                "Title": title_text,
                                "Link": download_url,
                                "TorrentLink": download_url,
                                "Size": 0,
                                "Tracker": "Vimm.net",
                                "Seeders": 0,
                                "CategoryDesc": "ROM",
                                "MagnetUri": "",
                            })
        
    except requests.exceptions.RequestException as e:
        # Network error - raise it so it shows in UI
        raise VimmError(f"Network error: {e}")
    except VimmError:
        # Re-raise VimmError so it shows in UI
        raise
    except Exception as e:
        # Other errors - raise with context
        raise VimmError(f"Parsing error: {e}")
    
    # Final check: if we had game links but no results, that's a problem
    if not results and game_links:
        raise VimmError(f"Found {len(game_links)} game links but couldn't extract download links from any game pages. This might indicate vimm.net changed their page structure. Check server logs for details.")
    
    # If no game links and no results, the error should have been raised above
    # But just in case, check here too (this should never happen if error handling is correct)
    if not results and not game_links:
        raise VimmError("No results found and no game links were extracted. This error should have been raised earlier - check code logic.")
    
    return results

