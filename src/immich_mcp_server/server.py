"""
Immich MCP Server — Photo management tools for Claude.

Part of the immich-photo-manager plugin.
License: MIT
"""

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp.server.fastmcp import FastMCP, Context

from .immich_client import ImmichClient


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Load every Immich account from the keyring; activate the default."""
    accounts = ImmichClient.load_all_accounts()  # may raise RuntimeError
    default_name = os.environ.get("IMMICH_DEFAULT_ACCOUNT", "")
    active_name = default_name if default_name in accounts else None

    if active_name:
        try:
            await accounts[active_name].ping()
        except Exception as e:
            print(
                f"Warning: Could not connect to Immich for account "
                f"'{active_name}' at {accounts[active_name].base_url}: {e}"
            )
    elif default_name:
        print(
            f"Warning: IMMICH_DEFAULT_ACCOUNT='{default_name}' not found in "
            f"keyring. No active account; tools will error until "
            f"switch_account() is called. Available: {list(accounts)}"
        )

    yield {
        "accounts": accounts,
        "active": active_name,
    }


mcp = FastMCP(
    "immich-photo-manager",
    instructions="Intelligent photo management for Immich. Search, curate albums, and publish galleries.",
    lifespan=app_lifespan,
)


def _client(ctx: Context) -> ImmichClient:
    """Get the currently active Immich client."""
    state = ctx.request_context.lifespan_context
    active = state.get("active")
    if not active:
        raise RuntimeError(
            "No active Immich account. Call list_accounts() to see options, "
            "then switch_account(name)."
        )
    return state["accounts"][active]


# ── Health & Stats ──────────────────────────────────────────


@mcp.tool()
async def ping(ctx: Context) -> str:
    """Check Immich server connectivity. Returns 'pong' if connected."""
    result = await _client(ctx).ping()
    return json.dumps(result)


@mcp.tool()
async def get_server_version(ctx: Context) -> str:
    """Get the Immich server version."""
    result = await _client(ctx).get_server_version()
    return json.dumps(result)


@mcp.tool()
async def get_statistics(ctx: Context) -> str:
    """Get library statistics: total photos, videos, and storage usage."""
    result = await _client(ctx).get_statistics()
    return json.dumps(result)


# ── Account Management ────────────────────────────────────


@mcp.tool()
async def list_accounts(ctx: Context) -> str:
    """List all configured Immich accounts and the currently active one."""
    state = ctx.request_context.lifespan_context
    accounts = state["accounts"]
    return json.dumps({
        "active": state.get("active"),
        "available": [
            {"name": name, "base_url": client.base_url}
            for name, client in accounts.items()
        ],
    })


@mcp.tool()
async def current_account(ctx: Context) -> str:
    """Return the currently active Immich account name and base URL."""
    state = ctx.request_context.lifespan_context
    active = state.get("active")
    if not active:
        return json.dumps({"active": None, "base_url": None})
    client = state["accounts"][active]
    return json.dumps({"active": active, "base_url": client.base_url})


@mcp.tool()
async def switch_account(ctx: Context, account: str) -> str:
    """Switch the active Immich account for the rest of this MCP session.

    The new account must already be loaded from the keyring. Restart reverts
    to the launcher's IMMICH_DEFAULT_ACCOUNT.

    Args:
        account: The account name (see list_accounts()).
    """
    state = ctx.request_context.lifespan_context
    accounts = state["accounts"]
    if account not in accounts:
        return json.dumps({
            "success": False,
            "error": f"Unknown account '{account}'. Available: {list(accounts)}",
        })

    candidate = accounts[account]
    try:
        await candidate.ping()
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": (
                f"Could not connect to Immich for account '{account}' at "
                f"{candidate.base_url}: {e}. Active account unchanged."
            ),
        })

    state["active"] = account
    try:
        stats = await candidate.get_statistics()
        photos = stats.get("photos", "?")
        videos = stats.get("videos", "?")
    except Exception:
        photos = videos = "?"

    return json.dumps({
        "success": True,
        "active": account,
        "base_url": candidate.base_url,
        "photos": photos,
        "videos": videos,
    })


# ── Asset Info ──────────────────────────────────────────────


@mcp.tool()
async def get_asset_info(ctx: Context, asset_id: str) -> str:
    """Get full metadata for a specific asset (EXIF, GPS, dates, camera, etc).

    Args:
        asset_id: The unique ID of the asset.
    """
    result = await _client(ctx).get_asset(asset_id)
    return json.dumps(result, default=str)


@mcp.tool()
async def update_asset_metadata(
    ctx: Context,
    asset_id: str,
    date_time_original: str = "",
    latitude: float | None = None,
    longitude: float | None = None,
    description: str = "",
    is_favorite: bool | None = None,
    rating: int | None = None,
) -> str:
    """Update metadata for a specific asset (dates, GPS coordinates, description, etc).
    Only provided fields are updated — omitted fields are left unchanged.

    Args:
        asset_id: The unique ID of the asset.
        date_time_original: ISO 8601 date string (e.g. '2019-07-14T15:23:41.000Z').
        latitude: GPS latitude (-90 to 90).
        longitude: GPS longitude (-180 to 180).
        description: Asset description text.
        is_favorite: Mark as favorite.
        rating: Rating from 1-5, or null for unrated.
    """
    fields: dict = {}
    if date_time_original:
        fields["dateTimeOriginal"] = date_time_original
    if latitude is not None:
        fields["latitude"] = latitude
    if longitude is not None:
        fields["longitude"] = longitude
    if description:
        fields["description"] = description
    if is_favorite is not None:
        fields["isFavorite"] = is_favorite
    if rating is not None:
        fields["rating"] = rating
    if not fields:
        return json.dumps({"error": "No fields to update. Provide at least one field."})
    result = await _client(ctx).update_asset(asset_id, **fields)
    return json.dumps(result, default=str)


@mcp.tool()
async def rotate_assets(
    ctx: Context,
    angle: int = 90,
    asset_ids: list[str] | None = None,
    album_id: str = "",
) -> str:
    """Rotate one or more assets. This is a non-destructive display transform —
    the original file is never modified.

    Provide EITHER asset_ids OR album_id. If album_id is given, all assets in
    that album are rotated.

    Args:
        angle: Rotation angle in degrees clockwise. Common values: 90, 180, 270. Default: 90.
        asset_ids: List of asset IDs to rotate.
        album_id: Rotate ALL assets in this album.
    """
    if angle % 90 != 0:
        return json.dumps({"error": "Angle must be a multiple of 90 (90, 180, 270)."})

    client = _client(ctx)

    # Resolve asset IDs from album if provided
    ids: list[str] = []
    album_name = ""
    if album_id:
        album = await client.get_album(album_id)
        album_name = album.get("albumName", "")
        ids = [a["id"] for a in album.get("assets", [])]
        if not ids:
            return json.dumps({"error": f"Album '{album_name}' is empty."})
    elif asset_ids:
        ids = asset_ids
    else:
        return json.dumps({"error": "Provide either asset_ids or album_id."})

    results: dict = {"rotated": 0, "failed": 0, "errors": []}
    for aid in ids:
        try:
            # Read current rotation and accumulate
            current_angle = 0
            try:
                edits = await client.get_asset_edits(aid)
                for edit in edits.get("edits", []):
                    if edit.get("action") == "rotate":
                        current_angle = edit["parameters"].get("angle", 0)
            except Exception:
                pass
            new_angle = (current_angle + angle) % 360
            if new_angle == 0:
                # Full circle — remove edits instead
                await client.delete_asset_edits(aid)
            else:
                await client.apply_asset_edits(aid, [
                    {"action": "rotate", "parameters": {"angle": new_angle}},
                ])
            results["rotated"] += 1
        except Exception as e:
            results["failed"] += 1
            results["errors"].append({"asset_id": aid, "error": str(e)})

    results["angle"] = angle
    results["total_requested"] = len(ids)
    if album_name:
        results["album"] = album_name
    if not results["errors"]:
        del results["errors"]
    return json.dumps(results, default=str)


@mcp.tool()
async def revert_asset_edits(
    ctx: Context,
    asset_ids: list[str] | None = None,
    album_id: str = "",
) -> str:
    """Remove all non-destructive edits (rotation, crop, mirror) from assets,
    reverting them to their original appearance.

    Provide EITHER asset_ids OR album_id.

    Args:
        asset_ids: List of asset IDs to revert.
        album_id: Revert ALL assets in this album.
    """
    client = _client(ctx)

    ids: list[str] = []
    album_name = ""
    if album_id:
        album = await client.get_album(album_id)
        album_name = album.get("albumName", "")
        ids = [a["id"] for a in album.get("assets", [])]
        if not ids:
            return json.dumps({"error": f"Album '{album_name}' is empty."})
    elif asset_ids:
        ids = asset_ids
    else:
        return json.dumps({"error": "Provide either asset_ids or album_id."})

    results: dict = {"reverted": 0, "failed": 0, "errors": []}
    for aid in ids:
        try:
            await client.delete_asset_edits(aid)
            results["reverted"] += 1
        except Exception as e:
            results["failed"] += 1
            results["errors"].append({"asset_id": aid, "error": str(e)})

    results["total_requested"] = len(ids)
    if album_name:
        results["album"] = album_name
    if not results["errors"]:
        del results["errors"]
    return json.dumps(results, default=str)


@mcp.tool()
async def get_map_markers(
    ctx: Context,
    file_created_after: str = "",
    file_created_before: str = "",
    is_favorite: bool | None = None,
) -> str:
    """Get all GPS map markers from the library. Returns asset IDs with lat/lon coordinates.
    Use this to discover all geographic locations in the photo library.

    Args:
        file_created_after: Optional ISO date filter (e.g. '2023-01-01').
        file_created_before: Optional ISO date filter.
        is_favorite: Filter favorites only.
    """
    result = await _client(ctx).get_map_markers(
        file_created_after=file_created_after or None,
        file_created_before=file_created_before or None,
        is_favorite=is_favorite,
    )
    return json.dumps({"total": len(result), "markers": result[:500]}, default=str)


# ── Search ──────────────────────────────────────────────────


@mcp.tool()
async def search_metadata(
    ctx: Context,
    city: str = "",
    state: str = "",
    country: str = "",
    make: str = "",
    model: str = "",
    taken_after: str = "",
    taken_before: str = "",
    is_favorite: bool | None = None,
    asset_type: str = "",
    page: int = 1,
    size: int = 50,
) -> str:
    """Search photos by EXIF metadata: location (city/state/country), camera (make/model),
    date range, favorites, and type (IMAGE/VIDEO).

    Args:
        city: Filter by city name (e.g. 'Barcelona', 'Cairo').
        state: Filter by state/region.
        country: Filter by country (e.g. 'Spain', 'Egypt').
        make: Camera manufacturer (e.g. 'Apple', 'Canon').
        model: Camera model (e.g. 'iPhone 14 Pro').
        taken_after: ISO date — only photos after this date.
        taken_before: ISO date — only photos before this date.
        is_favorite: Filter favorites only.
        asset_type: 'IMAGE' or 'VIDEO'.
        page: Page number (default 1).
        size: Results per page (default 50, max 200).
    """
    result = await _client(ctx).search_metadata(
        city=city or None,
        state=state or None,
        country=country or None,
        make=make or None,
        model=model or None,
        taken_after=taken_after or None,
        taken_before=taken_before or None,
        is_favorite=is_favorite,
        asset_type=asset_type or None,
        page=page,
        size=min(size, 200),
    )
    # Flatten the response for easier consumption
    assets = result.get("assets", {}).get("items", [])
    total = result.get("assets", {}).get("total", 0)
    return json.dumps({"total": total, "page": page, "assets": assets}, default=str)


@mcp.tool()
async def search_smart(
    ctx: Context,
    query: str,
    city: str = "",
    state: str = "",
    country: str = "",
    taken_after: str = "",
    taken_before: str = "",
    page: int = 1,
    size: int = 50,
) -> str:
    """AI-powered visual search using CLIP. Describe what you're looking for
    in natural language (e.g. 'sunset at the beach', 'birthday cake', 'mountain landscape').

    Can be combined with location and date filters.

    Args:
        query: Natural language description of what to find.
        city: Optional city filter.
        state: Optional state/region filter.
        country: Optional country filter.
        taken_after: ISO date — only photos after this date.
        taken_before: ISO date — only photos before this date.
        page: Page number (default 1).
        size: Results per page (default 50, max 200).
    """
    try:
        result = await _client(ctx).search_smart(
            query=query,
            city=city or None,
            state=state or None,
            country=country or None,
            taken_after=taken_after or None,
            taken_before=taken_before or None,
            page=page,
            size=min(size, 200),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 500:
            return json.dumps({
                "error": "Smart search is not available on this Immich server.",
                "detail": (
                    "The Immich machine learning service may not be running, "
                    "or Smart Search (CLIP) is disabled. "
                    "Enable it in Administration > Settings > Machine Learning Settings > Smart Search. "
                    "See https://immich.app/docs/features/smart-search for details."
                ),
                "http_status": 500,
            })
        raise
    assets = result.get("assets", {}).get("items", [])
    total = result.get("assets", {}).get("total", 0)
    return json.dumps({"total": total, "page": page, "assets": assets}, default=str)


# ── Albums ──────────────────────────────────────────────────


@mcp.tool()
async def list_albums(ctx: Context, shared: bool | None = None) -> str:
    """List all albums with their asset counts.

    Args:
        shared: Filter by shared status. None = all albums.
    """
    result = await _client(ctx).list_albums(shared=shared)
    albums = [
        {
            "id": a["id"],
            "albumName": a.get("albumName", ""),
            "description": a.get("description", ""),
            "assetCount": a.get("assetCount", 0),
            "shared": a.get("shared", False),
            "hasSharedLink": a.get("hasSharedLink", False),
            "createdAt": a.get("createdAt", ""),
        }
        for a in result
    ]
    return json.dumps({"total": len(albums), "albums": albums}, default=str)


@mcp.tool()
async def get_album(ctx: Context, album_id: str) -> str:
    """Get album details including all asset IDs.

    Args:
        album_id: The album's unique ID.
    """
    result = await _client(ctx).get_album(album_id)
    assets = result.get("assets", [])
    asset_ids = [a["id"] for a in assets]
    return json.dumps(
        {
            "id": result["id"],
            "albumName": result.get("albumName", ""),
            "description": result.get("description", ""),
            "assetCount": result.get("assetCount", 0),
            "shared": result.get("shared", False),
            "hasSharedLink": result.get("hasSharedLink", False),
            "createdAt": result.get("createdAt", ""),
            "updatedAt": result.get("updatedAt", ""),
            "asset_ids": asset_ids,
        },
        default=str,
    )


@mcp.tool()
async def create_album(
    ctx: Context, name: str, description: str = "", asset_ids: list[str] | None = None
) -> str:
    """Create a new album.

    Args:
        name: Album name (e.g. 'Roma, Italia').
        description: Optional description.
        asset_ids: Optional list of asset IDs to add immediately.
    """
    result = await _client(ctx).create_album(
        name=name, description=description, asset_ids=asset_ids
    )
    return json.dumps(
        {
            "id": result["id"],
            "albumName": result.get("albumName", ""),
            "assetCount": result.get("assetCount", 0),
        },
        default=str,
    )


@mcp.tool()
async def update_album(
    ctx: Context, album_id: str, name: str = "", description: str = ""
) -> str:
    """Update an album's name or description.

    Args:
        album_id: The album's unique ID.
        name: New name (empty = don't change).
        description: New description (empty = don't change).
    """
    result = await _client(ctx).update_album(
        album_id=album_id,
        name=name or None,
        description=description if description else None,
    )
    return json.dumps(result, default=str)


@mcp.tool()
async def delete_album(ctx: Context, album_id: str) -> str:
    """Delete an album. Photos are NOT deleted, only the album container.

    Args:
        album_id: The album's unique ID.
    """
    await _client(ctx).delete_album(album_id)
    return json.dumps({"deleted": True, "album_id": album_id})


@mcp.tool()
async def add_assets_to_album(ctx: Context, album_id: str, asset_ids: list[str]) -> str:
    """Add photos/videos to an album.

    Args:
        album_id: Target album ID.
        asset_ids: List of asset IDs to add.
    """
    result = await _client(ctx).add_assets_to_album(album_id, asset_ids)
    return json.dumps({"album_id": album_id, "added": len(asset_ids), "result": result}, default=str)


@mcp.tool()
async def remove_assets_from_album(ctx: Context, album_id: str, asset_ids: list[str]) -> str:
    """Remove photos/videos from an album. The photos themselves are NOT deleted.

    Args:
        album_id: Target album ID.
        asset_ids: List of asset IDs to remove.
    """
    result = await _client(ctx).remove_assets_from_album(album_id, asset_ids)
    return json.dumps({"album_id": album_id, "removed": len(asset_ids), "result": result}, default=str)


# ── Thumbnails ──────────────────────────────────────────────


@mcp.tool()
async def get_asset_thumbnail(ctx: Context, asset_id: str, size: str = "thumbnail") -> str:
    """Get a base64-encoded thumbnail for a single asset.
    Returns JSON with 'data' (base64 string) and 'type' (mime type).
    Size can be 'thumbnail' (250px, fast) or 'preview' (1440px, larger).

    Args:
        asset_id: The unique ID of the asset.
        size: 'thumbnail' (250px) or 'preview' (1440px). Default: thumbnail.
    """
    result = await _client(ctx).get_asset_thumbnail(asset_id, size)
    return json.dumps(result)


@mcp.tool()
async def get_album_thumbnails(
    ctx: Context, album_id: str, size: str = "thumbnail", limit: int = 20
) -> str:
    """Get base64-encoded thumbnails for all photos in an album (up to limit).
    Returns album info and a list of thumbnail entries with asset IDs, base64 data,
    filenames, and dates. Used for generating visual HTML galleries.

    Args:
        album_id: The album's unique ID.
        size: 'thumbnail' (250px) or 'preview' (1440px). Default: thumbnail.
        limit: Maximum number of thumbnails to fetch (default 20, max 50).
    """
    result = await _client(ctx).get_album_thumbnails(
        album_id, size, min(limit, 50)
    )
    return json.dumps(result, default=str)


@mcp.tool()
async def get_thumbnails_batch(
    ctx: Context, asset_ids: list[str], size: str = "thumbnail", limit: int = 20
) -> str:
    """Get base64-encoded thumbnails for a list of asset IDs WITHOUT needing an album.
    Use this when you have search results (asset IDs) and want to display them visually
    without creating a temporary album. Returns thumbnail entries with asset IDs, base64 data,
    filenames, and dates.

    Args:
        asset_ids: List of asset IDs to fetch thumbnails for.
        size: 'thumbnail' (250px) or 'preview' (1440px). Default: thumbnail.
        limit: Maximum number of thumbnails to fetch (default 20, max 50).
    """
    result = await _client(ctx).get_thumbnails_batch(
        asset_ids, size, min(limit, 50)
    )
    return json.dumps(result, default=str)


# ── Shared Links ────────────────────────────────────────────


@mcp.tool()
async def list_shared_links(ctx: Context) -> str:
    """List all shared links (public URLs for albums/assets)."""
    result = await _client(ctx).list_shared_links()
    links = [
        {
            "id": link["id"],
            "key": link.get("key", ""),
            "type": link.get("type", ""),
            "description": link.get("description", ""),
            "album_id": link.get("album", {}).get("id", "") if link.get("album") else "",
            "album_name": link.get("album", {}).get("albumName", "") if link.get("album") else "",
        }
        for link in result
    ]
    return json.dumps({"total": len(links), "links": links}, default=str)


@mcp.tool()
async def create_shared_link(
    ctx: Context,
    album_id: str,
    allow_download: bool = True,
    show_metadata: bool = True,
    description: str = "",
) -> str:
    """Create a public shared link for an album. This makes the album visible
    in the Immich Gallery frontend.

    Args:
        album_id: The album to share.
        allow_download: Allow visitors to download photos.
        show_metadata: Show EXIF metadata to visitors.
        description: Optional link description.
    """
    result = await _client(ctx).create_shared_link(
        album_id=album_id,
        allow_download=allow_download,
        show_metadata=show_metadata,
        description=description,
    )
    return json.dumps(
        {
            "id": result.get("id", ""),
            "key": result.get("key", ""),
            "album_id": album_id,
            "url": f"{_client(ctx).base_url}/share/{result.get('key', '')}",
        },
        default=str,
    )



@mcp.tool()
async def get_connection_info(ctx: Context) -> str:
    """Return the Immich base URL and a masked API key.  Used by skills to
    populate the {{IMMICH_URL}} placeholder in gallery templates.  The API
    key is intentionally masked — thumbnails are delivered as base64 data
    URIs, so the plaintext key is never needed in generated HTML.
    """
    client = _client(ctx)
    key = client.api_key
    masked = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"
    return json.dumps(
        {"base_url": client.base_url, "api_key_masked": masked},
        default=str,
    )


# ── People & Faces ─────────────────────────────────────────


@mcp.tool()
async def list_people(
    ctx: Context, page: int = 1, size: int = 50, with_hidden: bool = False
) -> str:
    """List all recognized people in the library (paginated).

    Args:
        page: Page number (default 1).
        size: Results per page (default 50).
        with_hidden: Include hidden people (default False).
    """
    result = await _client(ctx).list_people(page=page, size=size, with_hidden=with_hidden)
    people = result.get("people", [])
    total = result.get("total", len(people))
    return json.dumps({"total": total, "page": page, "people": people}, default=str)


@mcp.tool()
async def get_person(ctx: Context, person_id: str) -> str:
    """Get full details for a specific person.

    Args:
        person_id: The person's unique ID.
    """
    result = await _client(ctx).get_person(person_id)
    return json.dumps(result, default=str)


@mcp.tool()
async def update_person(
    ctx: Context,
    person_id: str,
    name: str = "",
    birth_date: str = "",
    is_hidden: bool | None = None,
    is_favorite: bool | None = None,
    feature_face_asset_id: str = "",
    color: str = "",
) -> str:
    """Update a person's details. Only provided fields are changed.

    Args:
        person_id: The person's unique ID.
        name: Display name for this person.
        birth_date: Birth date in ISO format (e.g. '1990-05-15').
        is_hidden: Hide this person from the People view.
        is_favorite: Mark this person as a favorite.
        feature_face_asset_id: Asset ID to use as the person's feature face.
        color: Color label for this person.
    """
    fields: dict = {}
    if name:
        fields["name"] = name
    if birth_date:
        fields["birthDate"] = birth_date
    if is_hidden is not None:
        fields["isHidden"] = is_hidden
    if is_favorite is not None:
        fields["isFavorite"] = is_favorite
    if feature_face_asset_id:
        fields["featureFaceAssetId"] = feature_face_asset_id
    if color:
        fields["color"] = color
    if not fields:
        return json.dumps({"error": "No fields to update. Provide at least one field."})
    result = await _client(ctx).update_person(person_id, **fields)
    return json.dumps(result, default=str)


@mcp.tool()
async def merge_people(ctx: Context, person_id: str, merge_ids: list[str]) -> str:
    """Merge multiple people into one. DESTRUCTIVE: the people in merge_ids
    are permanently absorbed into person_id. All their face assignments are
    transferred to the target person. This cannot be undone.

    Args:
        person_id: The target person to keep (all faces merge into this person).
        merge_ids: List of person IDs to merge into the target. These people will cease to exist.
    """
    result = await _client(ctx).merge_people(person_id, merge_ids)
    return json.dumps(result, default=str)


@mcp.tool()
async def search_people(ctx: Context, name: str, with_hidden: bool = False) -> str:
    """Search for people by name.

    Args:
        name: Name or partial name to search for.
        with_hidden: Include hidden people in results (default False).
    """
    result = await _client(ctx).search_people(name, with_hidden=with_hidden)
    return json.dumps(result, default=str)


@mcp.tool()
async def get_person_thumbnail(ctx: Context, person_id: str) -> str:
    """Get a base64-encoded face thumbnail for a person.
    Returns JSON with 'data' (base64 string) and 'type' (mime type).

    Args:
        person_id: The person's unique ID.
    """
    result = await _client(ctx).get_person_thumbnail(person_id)
    return json.dumps(result)


@mcp.tool()
async def get_asset_faces(ctx: Context, asset_id: str) -> str:
    """Get all detected faces in a specific asset, with their person assignments.

    Args:
        asset_id: The asset's unique ID.
    """
    result = await _client(ctx).get_asset_faces(asset_id)
    return json.dumps(result, default=str)


@mcp.tool()
async def reassign_face(ctx: Context, face_id: str, person_id: str) -> str:
    """Reassign a detected face to a different person. Use this to correct
    face recognition mistakes.

    Args:
        face_id: The face detection ID (from get_asset_faces).
        person_id: The person to assign this face to.
    """
    result = await _client(ctx).reassign_face(face_id, person_id)
    return json.dumps(result, default=str)


# ── Trash ──────────────────────────────────────────────────


@mcp.tool()
async def delete_assets(ctx: Context, asset_ids: list[str], force: bool = False) -> str:
    """Delete assets by moving them to trash, or permanently delete them.

    By default (force=False), assets are moved to trash and can be restored.
    With force=True, assets are PERMANENTLY DELETED and cannot be recovered.

    Args:
        asset_ids: List of asset IDs to delete.
        force: If True, permanently delete. If False (default), move to trash.
    """
    await _client(ctx).delete_assets(asset_ids, force=force)
    return json.dumps({
        "deleted": len(asset_ids),
        "force": force,
        "warning": "Assets permanently deleted." if force else "Assets moved to trash. Use restore_assets to undo.",
    })


@mcp.tool()
async def empty_trash(ctx: Context) -> str:
    """Permanently delete ALL assets currently in the trash.
    WARNING: This is IRREVERSIBLE. All trashed assets will be permanently destroyed.
    """
    await _client(ctx).empty_trash()
    return json.dumps({"success": True, "warning": "All trashed assets have been permanently deleted."})


@mcp.tool()
async def restore_trash(ctx: Context) -> str:
    """Restore ALL trashed assets back to the library."""
    await _client(ctx).restore_trash()
    return json.dumps({"success": True, "message": "All trashed assets have been restored."})


@mcp.tool()
async def restore_assets(ctx: Context, asset_ids: list[str]) -> str:
    """Restore specific assets from trash back to the library.

    Args:
        asset_ids: List of asset IDs to restore from trash.
    """
    await _client(ctx).restore_assets(asset_ids)
    return json.dumps({"restored": len(asset_ids)})


# ── Duplicates ─────────────────────────────────────────────


@mcp.tool()
async def get_duplicates(ctx: Context) -> str:
    """Get all ML-detected duplicate asset groups. Immich uses machine learning
    to identify visually similar photos. Returns groups of duplicate assets
    with similarity scores.
    """
    result = await _client(ctx).get_duplicates()
    return json.dumps(result, default=str)


@mcp.tool()
async def resolve_duplicates(ctx: Context, groups: list[dict]) -> str:
    """Resolve duplicate groups by specifying which assets to keep and which to trash.

    Each group dict must contain:
    - duplicateId: The duplicate group ID (from get_duplicates)
    - assetIds: List of asset IDs to KEEP
    - trashIds: List of asset IDs to move to TRASH

    Args:
        groups: List of resolution decisions, each with duplicateId, assetIds (keep), and trashIds (trash).
    """
    await _client(ctx).resolve_duplicates(groups)
    return json.dumps({
        "resolved": len(groups),
        "message": "Duplicate groups resolved. Trashed assets can be restored from trash.",
    })


# ── HTTP App (for Streamable HTTP transport) ────────────────

app = mcp.streamable_http_app()
