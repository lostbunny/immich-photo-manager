"""
Immich REST API client.
Wraps the Immich API endpoints needed for photo management.
"""

import base64
import json
import subprocess
import httpx
from typing import Any


KEYRING_SERVICE = "immich-mcp"


class ImmichClient:
    """Async HTTP client for the Immich REST API."""

    KEYRING_SERVICE = KEYRING_SERVICE

    def __init__(self, base_url: str, api_key: str):
        if not base_url or not api_key:
            raise ValueError("base_url and api_key are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._headers = {
            "x-api-key": self.api_key,
            "Accept": "application/json",
        }

    # ── Multi-account loading from GNOME Keyring ────────────

    @classmethod
    def load_all_accounts(cls) -> dict[str, "ImmichClient"]:
        """Enumerate every keyring entry with service=immich-mcp and build
        a {account_name: ImmichClient} dict.

        Raises RuntimeError if no entries are found (fail-fast on fresh
        install — the operator must store at least one account first).
        """
        raw = cls._secret_tool_search()
        parsed = cls._parse_keyring_search_output(raw)
        if not parsed:
            raise RuntimeError(
                "No Immich keyring entries found. Store at least one with: "
                "secret-tool store --label='Immich' service immich-mcp "
                "account <name> base_url <url>"
            )
        return {
            name: cls(base_url=attrs["base_url"], api_key=attrs["api_key"])
            for name, attrs in parsed.items()
            if attrs.get("base_url") and attrs.get("api_key")
        }

    @classmethod
    def _secret_tool_search(cls) -> str:
        """Run `secret-tool search --all service immich-mcp` and return stdout."""
        r = subprocess.run(
            ["secret-tool", "search", "--all", "service", cls.KEYRING_SERVICE],
            capture_output=True, text=True, check=False, timeout=5,
        )
        return r.stdout

    @staticmethod
    def _parse_keyring_search_output(text: str) -> dict[str, dict]:
        """Parse `secret-tool search --all` output into {account: {base_url, api_key}}.

        Each result block looks like:
            [/org/freedesktop/secrets/collection/login/123]
            label = Immich API key (account=tasha)
            secret = <api_key value>
            created = ...
            modified = ...
            schema = org.freedesktop.Secret.Generic
            attribute.account = tasha
            attribute.base_url = https://photos.example.com
            attribute.service = immich-mcp

        Blocks are separated by a blank line or by a new `[/org/...]` header.
        Blocks missing `attribute.account` are silently skipped.
        """
        accounts: dict[str, dict] = {}
        current: dict = {}

        def _flush():
            if current.get("account"):
                accounts[current["account"]] = {
                    "base_url": current.get("base_url", ""),
                    "api_key":  current.get("api_key",  ""),
                }

        for line in text.splitlines():
            line = line.rstrip()
            if not line or line.startswith("["):
                _flush()
                current = {}
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key == "secret":
                current["api_key"] = value
            elif key == "attribute.account":
                current["account"] = value
            elif key == "attribute.base_url":
                current["base_url"] = value
        _flush()
        return accounts

    # ── HTTP ────────────────────────────────────────────────

    async def _request(
        self, method: str, path: str, json: dict | None = None, params: dict | None = None
    ) -> Any:
        """Make an authenticated request to the Immich API."""
        url = f"{self.base_url}/api{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method, url, headers=self._headers, json=json, params=params
            )
            response.raise_for_status()
            if response.status_code == 204:
                return None
            return response.json()

    # ── Health ──────────────────────────────────────────────

    async def ping(self) -> dict:
        """Check server connectivity."""
        return await self._request("GET", "/server/ping")

    async def get_server_version(self) -> dict:
        """Get Immich server version."""
        return await self._request("GET", "/server/version")

    async def get_statistics(self) -> dict:
        """Get library statistics (photos, videos, storage)."""
        return await self._request("GET", "/server/statistics")

    # ── Assets ──────────────────────────────────────────────

    async def get_asset(self, asset_id: str) -> dict:
        """Get full metadata for a single asset."""
        return await self._request("GET", f"/assets/{asset_id}")

    async def update_asset(self, asset_id: str, **fields: Any) -> dict:
        """Update asset metadata (dates, GPS, description, etc)."""
        return await self._request("PUT", f"/assets/{asset_id}", json=fields)

    async def run_asset_job(self, asset_ids: list[str], name: str) -> None:
        """Queue a job for specific assets (e.g. regenerate-thumbnail)."""
        await self._request(
            "POST", "/assets/jobs", json={"name": name, "assetIds": asset_ids}
        )

    async def get_map_markers(
        self,
        is_archived: bool = False,
        is_favorite: bool | None = None,
        file_created_after: str | None = None,
        file_created_before: str | None = None,
    ) -> list[dict]:
        """Get all GPS markers from the library (for geographic discovery)."""
        params: dict[str, Any] = {"isArchived": str(is_archived).lower()}
        if is_favorite is not None:
            params["isFavorite"] = str(is_favorite).lower()
        if file_created_after:
            params["fileCreatedAfter"] = file_created_after
        if file_created_before:
            params["fileCreatedBefore"] = file_created_before
        return await self._request("GET", "/map/markers", params=params)

    # ── Search ──────────────────────────────────────────────

    async def search_metadata(
        self,
        city: str | None = None,
        state: str | None = None,
        country: str | None = None,
        make: str | None = None,
        model: str | None = None,
        taken_after: str | None = None,
        taken_before: str | None = None,
        is_favorite: bool | None = None,
        is_archived: bool | None = None,
        asset_type: str | None = None,
        page: int = 1,
        size: int = 100,
    ) -> dict:
        """Search assets by EXIF metadata (location, camera, dates)."""
        body: dict[str, Any] = {"page": page, "size": size}
        if city:
            body["city"] = city
        if state:
            body["state"] = state
        if country:
            body["country"] = country
        if make:
            body["make"] = make
        if model:
            body["model"] = model
        if taken_after:
            body["takenAfter"] = taken_after
        if taken_before:
            body["takenBefore"] = taken_before
        if is_favorite is not None:
            body["isFavorite"] = is_favorite
        if is_archived is not None:
            body["isArchived"] = is_archived
        if asset_type:
            body["type"] = asset_type
        return await self._request("POST", "/search/metadata", json=body)

    async def search_smart(
        self,
        query: str,
        city: str | None = None,
        state: str | None = None,
        country: str | None = None,
        taken_after: str | None = None,
        taken_before: str | None = None,
        page: int = 1,
        size: int = 100,
    ) -> dict:
        """AI-powered semantic search using CLIP (e.g. 'sunset at the beach')."""
        body: dict[str, Any] = {"query": query, "page": page, "size": size}
        if city:
            body["city"] = city
        if state:
            body["state"] = state
        if country:
            body["country"] = country
        if taken_after:
            body["takenAfter"] = taken_after
        if taken_before:
            body["takenBefore"] = taken_before
        return await self._request("POST", "/search/smart", json=body)

    # ── Albums ──────────────────────────────────────────────

    async def list_albums(self, shared: bool | None = None) -> list[dict]:
        """List all albums."""
        params = {}
        if shared is not None:
            params["shared"] = str(shared).lower()
        return await self._request("GET", "/albums", params=params)

    async def get_album(self, album_id: str) -> dict:
        """Get album details including all assets."""
        return await self._request("GET", f"/albums/{album_id}")

    async def create_album(
        self, name: str, description: str = "", asset_ids: list[str] | None = None
    ) -> dict:
        """Create a new album."""
        body: dict[str, Any] = {"albumName": name, "description": description}
        if asset_ids:
            body["assetIds"] = asset_ids
        return await self._request("POST", "/albums", json=body)

    async def update_album(
        self, album_id: str, name: str | None = None, description: str | None = None
    ) -> dict:
        """Update album name or description."""
        body: dict[str, Any] = {}
        if name:
            body["albumName"] = name
        if description is not None:
            body["description"] = description
        return await self._request("PATCH", f"/albums/{album_id}", json=body)

    async def delete_album(self, album_id: str) -> None:
        """Delete an album (does NOT delete photos)."""
        await self._request("DELETE", f"/albums/{album_id}")

    async def add_assets_to_album(self, album_id: str, asset_ids: list[str]) -> list[dict]:
        """Add assets to an album."""
        return await self._request(
            "PUT", f"/albums/{album_id}/assets", json={"ids": asset_ids}
        )

    async def remove_assets_from_album(
        self, album_id: str, asset_ids: list[str]
    ) -> list[dict]:
        """Remove assets from an album."""
        return await self._request(
            "DELETE", f"/albums/{album_id}/assets", json={"ids": asset_ids}
        )

    # ── Thumbnails ──────────────────────────────────────────

    async def get_asset_thumbnail(
        self, asset_id: str, size: str = "thumbnail", edited: bool = True
    ) -> dict:
        """Get a base64-encoded thumbnail for an asset.

        Args:
            asset_id: The asset ID.
            size: 'thumbnail' (250px) or 'preview' (1440px).
            edited: If True, return the edited version (with rotation/crop applied).

        Returns:
            dict with 'data' (base64 string) and 'type' (mime type).
        """
        url = f"{self.base_url}/api/assets/{asset_id}/{size}"
        params = {"edited": "true"} if edited else {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._headers, params=params)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "image/webp")
            b64 = base64.b64encode(response.content).decode("ascii")
            return {"data": b64, "type": content_type}

    async def get_album_thumbnails(
        self, album_id: str, size: str = "thumbnail", limit: int = 50
    ) -> dict:
        """Get base64 thumbnails for all assets in an album (up to limit).

        Args:
            album_id: The album ID.
            size: 'thumbnail' (250px) or 'preview' (1440px).
            limit: Max number of thumbnails to fetch.

        Returns:
            dict with album info and list of thumbnail entries.
        """
        album = await self.get_album(album_id)
        assets = album.get("assets", [])[:limit]
        thumbnails = []
        for asset in assets:
            aid = asset["id"]
            try:
                thumb = await self.get_asset_thumbnail(aid, size)
                thumbnails.append({
                    "id": aid,
                    "data": thumb["data"],
                    "type": thumb["type"],
                    "originalFileName": asset.get("originalFileName", ""),
                    "fileCreatedAt": asset.get("fileCreatedAt", ""),
                })
            except Exception:
                # Skip assets whose thumbnails can't be fetched
                continue
        return {
            "albumId": album_id,
            "albumName": album.get("albumName", ""),
            "totalAssets": album.get("assetCount", 0),
            "fetchedCount": len(thumbnails),
            "thumbnails": thumbnails,
        }

    async def get_thumbnails_batch(
        self, asset_ids: list[str], size: str = "thumbnail", limit: int = 50
    ) -> dict:
        """Get base64 thumbnails for a list of asset IDs (no album required).

        Args:
            asset_ids: List of asset IDs to fetch thumbnails for.
            size: 'thumbnail' (250px) or 'preview' (1440px).
            limit: Max number of thumbnails to fetch.

        Returns:
            dict with list of thumbnail entries.
        """
        ids_to_fetch = asset_ids[:limit]
        thumbnails = []
        for aid in ids_to_fetch:
            try:
                thumb = await self.get_asset_thumbnail(aid, size)
                # Try to get basic asset info for filename/date
                try:
                    asset_info = await self.get_asset(aid)
                    original_name = asset_info.get("originalFileName", "")
                    created_at = asset_info.get("fileCreatedAt", "")
                except Exception:
                    original_name = ""
                    created_at = ""
                thumbnails.append({
                    "id": aid,
                    "data": thumb["data"],
                    "type": thumb["type"],
                    "originalFileName": original_name,
                    "fileCreatedAt": created_at,
                })
            except Exception:
                continue
        return {
            "totalRequested": len(ids_to_fetch),
            "fetchedCount": len(thumbnails),
            "thumbnails": thumbnails,
        }

    # ── Shared Links ────────────────────────────────────────

    async def list_shared_links(self) -> list[dict]:
        """List all shared links."""
        return await self._request("GET", "/shared-links")

    async def create_shared_link(
        self,
        album_id: str,
        allow_download: bool = True,
        show_metadata: bool = True,
        allow_upload: bool = False,
        description: str = "",
    ) -> dict:
        """Create a shared link for an album (publishes to Gallery)."""
        body = {
            "type": "ALBUM",
            "albumId": album_id,
            "allowDownload": allow_download,
            "showMetadata": show_metadata,
            "allowUpload": allow_upload,
        }
        if description:
            body["description"] = description
        return await self._request("POST", "/shared-links", json=body)

    async def delete_shared_link(self, link_id: str) -> None:
        """Delete a shared link."""
        await self._request("DELETE", f"/shared-links/{link_id}")

    # ── People & Faces ─────────────────────────────────────

    async def list_people(
        self, page: int = 1, size: int = 50, with_hidden: bool = False
    ) -> dict:
        """List all people (paginated)."""
        params = {"page": str(page), "size": str(size), "withHidden": str(with_hidden).lower()}
        return await self._request("GET", "/people", params=params)

    async def get_person(self, person_id: str) -> dict:
        """Get a person's details."""
        return await self._request("GET", f"/people/{person_id}")

    async def update_person(self, person_id: str, **fields) -> dict:
        """Update a person (name, birthDate, isHidden, etc)."""
        return await self._request("PUT", f"/people/{person_id}", json=fields)

    async def merge_people(self, person_id: str, merge_ids: list[str]) -> dict:
        """Merge multiple people into one."""
        return await self._request(
            "POST", f"/people/{person_id}/merge", json={"ids": merge_ids}
        )

    async def get_person_statistics(self, person_id: str) -> dict:
        """Get asset count for a person."""
        return await self._request("GET", f"/people/{person_id}/statistics")

    async def search_people(self, name: str, with_hidden: bool = False) -> list[dict]:
        """Search people by name."""
        params = {"name": name, "withHidden": str(with_hidden).lower()}
        return await self._request("GET", "/search/person", params=params)

    async def get_person_thumbnail(self, person_id: str) -> dict:
        """Get a base64-encoded face thumbnail for a person."""
        url = f"{self.base_url}/api/people/{person_id}/thumbnail"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._headers)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "image/jpeg")
            b64 = base64.b64encode(response.content).decode("ascii")
            return {"data": b64, "type": content_type}

    async def get_asset_faces(self, asset_id: str) -> list[dict]:
        """Get all detected faces for an asset."""
        return await self._request("GET", "/faces", params={"id": asset_id})

    async def reassign_face(self, face_id: str, person_id: str) -> dict:
        """Reassign a face to a different person."""
        return await self._request("PUT", f"/faces/{face_id}", json={"id": person_id})

    # ── Asset Edits (rotate, mirror, crop) ───────────────────

    async def apply_asset_edits(self, asset_id: str, edits: list[dict]) -> dict | None:
        """Apply non-destructive edits (rotate, mirror, crop) to an asset."""
        return await self._request(
            "PUT", f"/assets/{asset_id}/edits", json={"edits": edits}
        )

    async def get_asset_edits(self, asset_id: str) -> dict:
        """Get current edits applied to an asset."""
        return await self._request("GET", f"/assets/{asset_id}/edits")

    async def delete_asset_edits(self, asset_id: str) -> None:
        """Remove all edits from an asset (revert to original)."""
        await self._request("DELETE", f"/assets/{asset_id}/edits")

    # ── Trash ──────────────────────────────────────────────

    async def delete_assets(self, asset_ids: list[str], force: bool = False) -> None:
        """Delete or trash assets."""
        await self._request("DELETE", "/assets", json={"ids": asset_ids, "force": force})

    async def empty_trash(self) -> None:
        """Permanently empty the trash."""
        await self._request("POST", "/trash/empty")

    async def restore_trash(self) -> None:
        """Restore all trashed assets."""
        await self._request("POST", "/trash/restore")

    async def restore_assets(self, asset_ids: list[str]) -> None:
        """Restore specific assets from trash."""
        await self._request("POST", "/trash/restore/assets", json={"ids": asset_ids})

    # ── Duplicates ─────────────────────────────────────────

    async def get_duplicates(self) -> list[dict]:
        """Get all detected duplicate groups."""
        return await self._request("GET", "/duplicates")

    async def resolve_duplicates(self, groups: list[dict]) -> None:
        """Resolve duplicate groups (keep/trash decisions)."""
        await self._request("POST", "/duplicates/resolve", json=groups)
