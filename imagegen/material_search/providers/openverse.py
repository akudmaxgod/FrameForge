"""Manual image search in the public Openverse catalog."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from ...domain import DomainError
from ..web_image_fetch import NoWebRedirect
from .base import MaterialSearchItem


def search_images(query: str, commercial: bool = True) -> list[dict]:
    params = {"q": query, "page_size": 20}
    if commercial:
        params["license"] = "by,by-sa,cc0,pdm"
    request = Request(
        "https://api.openverse.org/v1/images/?" + urlencode(params),
        headers={"Accept": "application/json", "User-Agent": "FrameForge/2.0 (manual image search)"},
    )
    try:
        with build_opener(ProxyHandler({}), NoWebRedirect()).open(request, timeout=12) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Openverse response too large")
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("Invalid Openverse results")
    except HTTPError as error:
        if error.code == 429:
            raise DomainError("Лимит Openverse исчерпан. Повторите поиск позже.", 429) from error
        raise DomainError(f"Openverse вернул HTTP {error.code}.", 502) from error
    except (URLError, OSError, ValueError) as error:
        raise DomainError("Не удалось получить ответ Openverse.", 502) from error

    items = []
    for result in data["results"][:20]:
        if not isinstance(result, dict):
            continue
        image = result.get("url")
        identifier = result.get("id")
        if not isinstance(image, str) or not image.startswith("https://") or not isinstance(identifier, str):
            continue
        license_code = result.get("license")
        version = result.get("license_version")
        if license_code == "pdm":
            license_name = "Public Domain Mark"
        elif license_code == "cc0":
            license_name = "CC0"
        elif license_code:
            license_name = f"CC {str(license_code).upper()} {version or ''}".strip()
        else:
            license_name = ""
        thumbnail = result.get("thumbnail")
        items.append(MaterialSearchItem(
            provider="openverse",
            external_id=identifier,
            media_type="photo",
            title=result.get("title") or "Изображение",
            download_url=image,
            thumbnail_url=thumbnail if isinstance(thumbnail, str) and thumbnail.startswith("https://") else image,
            source_page_url=result.get("foreign_landing_url") or "",
            author=result.get("creator") or "",
            author_url=result.get("creator_url") or "",
            width=result["width"] if type(result.get("width")) is int else 0,
            height=result["height"] if type(result.get("height")) is int else 0,
            license_name=license_name,
            license_url=result.get("license_url") or "",
            rights_status="recorded_license" if license_code else "unknown",
        ).to_dict())
    return items
