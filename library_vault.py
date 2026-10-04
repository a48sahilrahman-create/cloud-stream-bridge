"""
Library Vault Engine - Stateless Cloud Runner Persistence & Remote Sync
Features:
  - Local JSON Catalog Registry (library_vault.json)
  - Remote Sync: Fetch stream links from GitHub Gist, Raw GitHub URLs, or HTTP JSON endpoints
  - Selective Stream Staging: Choose which streams to mount into a fresh Cloud Shell runner
  - Zero-Loss Export & Import: Backup your entire stream catalog in 1 click
"""

import os
import json
import time
import logging
from typing import List, Dict, Any, Optional
import httpx

logger = logging.getLogger("library_vault")
VAULT_FILE = os.path.join(os.path.dirname(__file__), "library_vault.json")


class LibraryVault:
    def __init__(self, filepath: str = VAULT_FILE):
        self.filepath = filepath
        self.catalog: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.catalog = data
                    elif isinstance(data, list):
                        self.catalog = {item.get("id", f"item_{i}"): item for i, item in enumerate(data)}
            except Exception as e:
                logger.error(f"Failed to load library vault from {self.filepath}: {e}")
                self.catalog = {}
        else:
            self.catalog = {}

    def save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.catalog, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save library vault to {self.filepath}: {e}")

    def list_items(self) -> List[Dict[str, Any]]:
        return list(self.catalog.values())

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        return self.catalog.get(item_id)

    def add_or_update(
        self,
        url: str,
        title: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
        size_hint: Optional[str] = None,
        category: Optional[str] = "Movies"
    ) -> Dict[str, Any]:
        url_clean = url.strip()
        # Derive stable item ID from url hash
        item_id = f"lib_{abs(hash(url_clean))}"
        now = time.time()

        item = {
            "id": item_id,
            "url": url_clean,
            "title": title or "Stream Link",
            "custom_headers": custom_headers or {},
            "size_hint": size_hint or "Unknown",
            "category": category or "Movies",
            "updated_at": now
        }
        if item_id not in self.catalog:
            item["created_at"] = now
        else:
            item["created_at"] = self.catalog[item_id].get("created_at", now)

        self.catalog[item_id] = item
        self.save()
        return item

    def remove_item(self, item_id: str) -> bool:
        if item_id in self.catalog:
            del self.catalog[item_id]
            self.save()
            return True
        return False

    def clear(self) -> int:
        count = len(self.catalog)
        self.catalog.clear()
        self.save()
        return count

    def export_catalog(self) -> List[Dict[str, Any]]:
        return list(self.catalog.values())

    def import_catalog(self, items: List[Dict[str, Any]], merge: bool = True) -> int:
        if not merge:
            self.catalog.clear()
        added_count = 0
        for item in items:
            if isinstance(item, dict) and item.get("url"):
                self.add_or_update(
                    url=item["url"],
                    title=item.get("title"),
                    custom_headers=item.get("custom_headers"),
                    size_hint=item.get("size_hint"),
                    category=item.get("category", "Movies")
                )
                added_count += 1
        return added_count

    async def fetch_from_remote_url(self, remote_url: str, merge: bool = True) -> Dict[str, Any]:
        """
        Fetches stream catalog JSON from a remote GitHub Gist, raw GitHub repo URL, or Pastebin.
        """
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(remote_url)
            if resp.status_code != 200:
                raise ValueError(f"Remote returned HTTP {resp.status_code}")
            try:
                data = resp.json()
            except Exception:
                raise ValueError("Remote content is not valid JSON")

        if isinstance(data, dict) and "items" in data:
            items_to_import = data["items"]
        elif isinstance(data, list):
            items_to_import = data
        elif isinstance(data, dict):
            items_to_import = list(data.values())
        else:
            raise ValueError("Unrecognized JSON catalog schema")

        count = self.import_catalog(items_to_import, merge=merge)
        return {"imported_count": count, "total_catalog": len(self.catalog)}


vault = LibraryVault()
