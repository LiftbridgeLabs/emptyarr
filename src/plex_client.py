import requests
import defusedxml.ElementTree as ET
from typing import Optional, List, Dict


# Plex media type IDs
_MOVIE_TYPES = [1]        # movie
_TV_TYPES    = [2, 3, 4]  # show, season, episode

# Human-readable labels for type IDs
_TYPE_LABELS = {
    1: "movie",
    2: "show",
    3: "season",
    4: "episode",
}


class PlexClient:
    def __init__(self, url: str, token: str):
        self.url   = url.rstrip("/")
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "X-Plex-Token":             token,
            "X-Plex-Product":           "emptyarr",
            "X-Plex-Version":           "1.1.0",
            "X-Plex-Client-Identifier": "emptyarr",
            "Accept":                   "application/json",
        })

    def _get(self, path: str, params: dict = None, timeout: int = 15):
        return self.session.get(f"{self.url}{path}", params=params, timeout=timeout)

    def check_reachable(self) -> Dict:
        try:
            r = self._get("/identity")
            if r.status_code == 200:
                version = r.json().get("MediaContainer", {}).get("version", "?")
                return {"pass": True, "detail": f"Plex reachable (v{version}) at {self.url}"}
            return {"pass": False, "detail": f"Plex returned HTTP {r.status_code}"}
        except requests.exceptions.Timeout:
            return {"pass": False, "detail": f"Plex timed out: {self.url}"}
        except Exception as e:
            return {"pass": False, "detail": f"Plex unreachable ({self.url}): {e}"}

    def get_sections(self) -> List[Dict]:
        r = self._get("/library/sections")
        r.raise_for_status()
        return [
            {"id": str(s["key"]), "title": s["title"], "type": s["type"]}
            for s in r.json().get("MediaContainer", {}).get("Directory", [])
        ]

    def find_section_id(self, library_name: str) -> Optional[str]:
        try:
            for s in self.get_sections():
                if s["title"].lower() == library_name.lower():
                    return s["id"]
        except Exception:
            pass
        return None

    def get_section_type(self, section_id: str) -> Optional[str]:
        try:
            for s in self.get_sections():
                if s["id"] == section_id:
                    return s["type"]
        except Exception:
            pass
        return None

    def get_library_item_count(self, section_id: str) -> Optional[int]:
        """Return a count comparable to media files, or None when Plex fails.

        TV section `/all` counts shows, while paths normally contain episodes.
        Requesting episode type 4 gives a useful leaf-item safety threshold.
        """
        try:
            section_type = self.get_section_type(section_id)
            if section_type is None:
                return None
            params = {
                "X-Plex-Container-Start": 0,
                "X-Plex-Container-Size": 0,
            }
            if section_type == "show":
                params["type"] = 4
            r = self._get(f"/library/sections/{section_id}/all",
                          params=params)
            r.raise_for_status()
            return int(r.json().get("MediaContainer", {}).get("totalSize", 0))
        except Exception:
            return None

    def _fetch_deleted_xml(self, section_id: str,
                           type_id: int) -> Optional[List[Dict]]:
        """
        Fetch items with deletedAt using XML (required — JSON omits deletedAt
        on Media children for episodes). Checks both item-level and
        Media child-level deletedAt.
        Returns list of {title, year, type, deleted_at, media_type_id}.
        """
        try:
            r = requests.get(
                f"{self.url}/library/sections/{section_id}/all",
                params={
                    "checkFiles":   1,
                    "type":         type_id,
                    "X-Plex-Token": self.token,
                },
                timeout=120,
            )
            if r.status_code != 200:
                return None
            root    = ET.fromstring(r.text)
            deleted = []
            for item in root:
                # Check deletedAt on the item itself (shows, seasons)
                def _item_dict(deleted_at: int) -> Dict:
                    return {
                        "title":             item.get("title", "Unknown"),
                        "year":              item.get("year", ""),
                        "type":              _TYPE_LABELS.get(type_id, "item"),
                        "deleted_at":        deleted_at,
                        "media_type_id":     type_id,
                        "index":             item.get("index", ""),
                        "parent_title":      item.get("parentTitle", ""),
                        "parent_index":      item.get("parentIndex", ""),
                        "grandparent_title": item.get("grandparentTitle", ""),
                    }

                if item.get("deletedAt"):
                    deleted.append(_item_dict(int(item.get("deletedAt", 0))))
                else:
                    # Check deletedAt on <Media> children (episodes with
                    # unavailable/replaced file versions)
                    for media in item.findall("Media"):
                        if media.get("deletedAt"):
                            deleted.append(_item_dict(int(media.get("deletedAt", 0))))
                            break  # one entry per episode
            return deleted
        except Exception:
            return None

    def get_trash_items(self, section_id: str) -> Optional[List[Dict]]:
        """
        Get all items that will be removed by emptyTrash.
        Returns list of items with type info for breakdown reporting.
        """
        try:
            section_type = self.get_section_type(section_id)
            if section_type is None:
                return None
            type_ids     = _TV_TYPES if section_type == "show" else _MOVIE_TYPES

            all_items   = []
            seen_titles = set()

            for type_id in type_ids:
                fetched = self._fetch_deleted_xml(section_id, type_id)
                if fetched is None:
                    return None
                for item in fetched:
                    # Deduplicate by title+type
                    key = f"{item['title']}_{item['type']}"
                    if key not in seen_titles:
                        all_items.append(item)
                        seen_titles.add(key)

            # Also check legacy trash=1 endpoint and merge
            try:
                r_legacy = self._get(
                    f"/library/sections/{section_id}/all",
                    params={"trash": 1},
                )
                if r_legacy.status_code == 200:
                    for item in r_legacy.json().get("MediaContainer", {}).get("Metadata", []):
                        key = f"{item.get('title', '')}_{item.get('type', '')}"
                        if key not in seen_titles:
                            all_items.append({
                                "title": item.get("title", "Unknown"),
                                "year":  item.get("year", ""),
                                "type":  item.get("type", ""),
                                "media_type_id": 0,
                            })
            except Exception:
                pass

            return all_items
        except Exception:
            return None

    def clean_bundles(self) -> Dict:
        """
        Ask Plex to perform its server-wide Clean Bundles maintenance action.
        The runner keeps this opt-in because it is broader than a single
        library's Empty Trash operation.
        """
        try:
            r = self.session.put(
                f"{self.url}/library/clean",
                timeout=60
            )
            if r.status_code in (200, 202, 204):
                return {"ok": True}
            return {"ok": False, "http": r.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def empty_trash(self, section_id: str) -> Dict:
        try:
            r = self.session.put(
                f"{self.url}/library/sections/{section_id}/emptyTrash",
                timeout=30
            )
            if r.status_code in (200, 204):
                return {"ok": True,  "http": r.status_code}
            return {"ok": False, "http": r.status_code, "error": r.text[:200]}
        except Exception as e:
            return {"ok": False, "http": None, "error": str(e)}
