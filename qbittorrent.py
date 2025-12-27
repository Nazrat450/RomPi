import requests


class Qbit:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.s = requests.Session()

    def login(self):
        print(f"DEBUG: qBittorrent - Attempting login to {self.base_url}")
        r = self.s.post(
            f"{self.base_url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            timeout=15,
        )
        print(f"DEBUG: qBittorrent - Login response status: {r.status_code}")
        print(f"DEBUG: qBittorrent - Login response text: {r.text[:100]}")
        r.raise_for_status()
        # qB returns "Ok." on success (sometimes "Ok")
        if "Ok" not in r.text:
            print(f"DEBUG: qBittorrent - Login failed: {r.text}")
            raise RuntimeError(f"qBittorrent login failed: {r.text}")
        print(f"DEBUG: qBittorrent - Login successful")

    def add_urls(self, urls: str, *, savepath: str | None = None, category: str | None = None):
        """
        Add magnet links and/or direct torrent URLs via qBittorrent's /torrents/add.
        qB expects the field name "urls" with newline-separated URLs.
        """
        data = {"urls": urls}
        if savepath:
            data["savepath"] = savepath
        if category:
            data["category"] = category

        print(f"DEBUG: qBittorrent - Adding URL: {urls[:100]}...")
        
        # Get count BEFORE adding (for verification)
        count_before = None
        try:
            verify_r = self.s.get(
                f"{self.base_url}/api/v2/torrents/info",
                params={"filter": "all"},
                timeout=10,
            )
            verify_r.raise_for_status()
            torrents_before = verify_r.json()
            count_before = len(torrents_before)
            print(f"DEBUG: qBittorrent - Torrent count BEFORE adding: {count_before}")
        except Exception as e:
            print(f"DEBUG: qBittorrent - Could not get torrent count before: {e}")
        
        # Now try to add the torrent
        r = self.s.post(
            f"{self.base_url}/api/v2/torrents/add",
            data=data,
            timeout=30,
        )
        
        print(f"DEBUG: qBittorrent - Response status: {r.status_code}")
        print(f"DEBUG: qBittorrent - Response text: {r.text[:200]}")
        
        r.raise_for_status()
        
        # qBittorrent can return 200 OK but with error messages in the body
        response_text = (r.text or "").strip()
        
        # Check for common error responses
        if response_text.lower() in ("fails.", "fail", "error"):
            print(f"DEBUG: qBittorrent - Error detected: {response_text}")
            raise RuntimeError(f"qBittorrent rejected the torrent: {response_text}")
        
        # qB often returns empty string on success; sometimes "Ok."
        # If there's a non-empty response that's not "Ok" or "Ok.", it might be an error
        if response_text and response_text.lower() not in ("ok", "ok.", ""):
            # Check if it looks like an error message
            error_indicators = ["fail", "error", "invalid", "unauthorized", "forbidden", "bad", "unable"]
            if any(indicator in response_text.lower() for indicator in error_indicators):
                print(f"DEBUG: qBittorrent - Error detected in response: {response_text}")
                raise RuntimeError(f"qBittorrent returned an error: {response_text}")
        
        print(f"DEBUG: qBittorrent - qBittorrent returned success response: {response_text}")
        
        # Verify the torrent was actually added by checking the torrent list
        # qBittorrent can take a moment to update the count, so retry a few times
        import time
        count_after = None
        
        # Try checking multiple times with increasing delays
        for attempt in range(3):
            wait_time = 1.0 + (attempt * 0.5)  # 1.0s, 1.5s, 2.0s
            time.sleep(wait_time)
            
            try:
                verify_r = self.s.get(
                    f"{self.base_url}/api/v2/torrents/info",
                    params={"filter": "all"},
                    timeout=10,
                )
                verify_r.raise_for_status()
                torrents_after = verify_r.json()
                count_after = len(torrents_after)
                print(f"DEBUG: qBittorrent - Torrent count AFTER adding (attempt {attempt + 1}): {count_after}")
                
                # Check if count increased
                if count_before is not None and count_after is not None:
                    if count_after > count_before:
                        print(f"DEBUG: qBittorrent - ✅ VERIFICATION SUCCESS: Torrent count increased: {count_before} -> {count_after} (added {count_after - count_before} torrent(s))")
                        return response_text  # Success! Exit early
                    elif attempt < 2:
                        # Not increased yet, but we have more attempts
                        print(f"DEBUG: qBittorrent - Count not increased yet ({count_before} -> {count_after}), retrying...")
                        continue
                    else:
                        # Final attempt and still no increase - but only fail for Jackett proxy URLs
                        # (regular URLs might work even if count doesn't increase immediately)
                        if "/dl/" in urls or "jackett_apikey" in urls:
                            print(f"DEBUG: qBittorrent - ❌ VERIFICATION FAILED: Torrent count did not increase after {attempt + 1} attempts! ({count_before} -> {count_after})")
                            print(f"DEBUG: qBittorrent - qBittorrent returned '{response_text}' but torrent was not actually added")
                            raise RuntimeError("qBittorrent returned 'Ok.' but torrent was not actually added (count did not increase)")
                        else:
                            # For non-proxy URLs, assume it worked (might be a timing issue)
                            print(f"DEBUG: qBittorrent - Count didn't increase but assuming success for non-proxy URL")
                            return response_text
            except RuntimeError:
                # Re-raise our verification error
                raise
            except Exception as e:
                print(f"DEBUG: qBittorrent - Could not verify torrent addition (attempt {attempt + 1}): {e}")
                if attempt == 2:
                    # Final attempt failed, but we'll assume it worked (better than failing)
                    print(f"DEBUG: qBittorrent - Verification failed but assuming success (can't verify)")
        
        return response_text

    def add_url(self, url: str, *, savepath: str | None = None, category: str | None = None):
        return self.add_urls(url, savepath=savepath, category=category)

    def add_magnet(self, magnet: str, *, savepath: str | None = None, category: str | None = None):
        if not magnet.startswith("magnet:"):
            raise ValueError("add_magnet() requires a magnet: URI")
        return self.add_urls(magnet, savepath=savepath, category=category)

    def add_torrent_bytes(
        self,
        torrent_bytes: bytes,
        *,
        filename: str = "download.torrent",
        savepath: str | None = None,
        category: str | None = None,
    ):
        """
        Upload a .torrent file (bytes) to qBittorrent. This is the most reliable
        fallback when indexers don't provide MagnetUri and torrent URLs need auth/cookies.
        """
        if not torrent_bytes or len(torrent_bytes) < 50:
            raise ValueError("torrent_bytes looks empty/invalid")

        data = {}
        if savepath:
            data["savepath"] = savepath
        if category:
            data["category"] = category

        files = {
            # field name MUST be "torrents" for qBittorrent
            "torrents": (filename, torrent_bytes, "application/x-bittorrent"),
        }

        r = self.s.post(
            f"{self.base_url}/api/v2/torrents/add",
            data=data,
            files=files,
            timeout=30,
        )
        r.raise_for_status()
        
        # qBittorrent can return 200 OK but with error messages in the body
        response_text = (r.text or "").strip()
        
        # Check for common error responses
        if response_text.lower() in ("fails.", "fail", "error"):
            raise RuntimeError(f"qBittorrent rejected the torrent: {response_text}")
        
        # qB often returns empty string on success; sometimes "Ok."
        # If there's a non-empty response that's not "Ok" or "Ok.", it might be an error
        if response_text and response_text.lower() not in ("ok", "ok.", ""):
            # Check if it looks like an error message
            error_indicators = ["fail", "error", "invalid", "unauthorized", "forbidden"]
            if any(indicator in response_text.lower() for indicator in error_indicators):
                raise RuntimeError(f"qBittorrent returned an error: {response_text}")
        
        return response_text

