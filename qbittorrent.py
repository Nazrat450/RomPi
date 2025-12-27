import requests


class Qbit:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.s = requests.Session()

    def login(self):
        r = self.s.post(
            f"{self.base_url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            timeout=15,
        )
        r.raise_for_status()
        # qB returns "Ok." on success (sometimes "Ok")
        if "Ok" not in r.text:
            raise RuntimeError(f"qBittorrent login failed: {r.text}")

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

        r = self.s.post(
            f"{self.base_url}/api/v2/torrents/add",
            data=data,
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

