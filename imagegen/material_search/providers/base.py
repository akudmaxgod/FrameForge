"""Base contract and data structures for material search providers."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional
import threading

@dataclass
class MaterialSearchItem:
    provider: str
    external_id: str
    media_type: str  # 'photo' | 'video'
    title: str = ""
    description: str = ""
    source_page_url: str = ""
    preview_url: str = ""
    thumbnail_url: str = ""
    large_url: str = ""
    download_url: str = ""
    author: str = ""
    author_url: str = ""
    width: int = 0
    height: int = 0
    duration_ms: int = 0
    license_name: str = ""
    license_url: str = ""
    attribution_text: str = ""
    rights_status: str = "unknown"  # 'recorded_license' | 'permission_recorded' | 'unknown' | 'restricted'
    retrieved_at: str = ""
    canonical_source_id: str = ""  # provider:media_type:external_id
    provider_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.canonical_source_id:
            self.canonical_source_id = f"{self.provider}:{self.media_type}:{self.external_id}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MaterialSearchProvider(ABC):
    """Abstract base class for all material search adapters."""

    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.root = service.root

    @abstractmethod
    def capabilities(self) -> Dict[str, Any]:
        """Return provider features, limitations, supported media types and auth status."""
        pass

    @abstractmethod
    def search(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
        cursor: Optional[str] = None,
        cancellation: Optional[threading.Event] = None
    ) -> Dict[str, Any]:
        """
        Execute search query and return normalized results with pagination cursor.
        Must respect 24-hour search cache and rate limits.
        """
        pass

    @abstractmethod
    def resolve(self, external_id: str, media_type: str = "photo") -> Optional[MaterialSearchItem]:
        """Fetch fresh metadata, verify availability and safe direct download URL."""
        pass

    @abstractmethod
    def import_selected(
        self,
        project_id: str,
        external_id: str,
        media_type: str = "photo",
        options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Download file safely, validate decoded stream, add to project media library,
        and record origin/rights in media_library_sources.
        """
        pass
