"""Rights tracking, provenance management, and credits.txt/JSON generation."""
import json
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

class RightsManager:
    def __init__(self, service):
        self.service = service
        self.db = service.db

    def get_project_provenance(self, project_id: str) -> List[Dict[str, Any]]:
        """Retrieve all recorded sources for imported media in the project."""
        self.service.project(project_id)
        rows = self.db.all(
            """
            SELECT s.*, i.original_name, i.media_type, i.width, i.height, (i.duration_ms / 1000.0) AS duration_sec, i.duration_ms
            FROM media_library_sources s
            JOIN media_library_items i ON i.id = s.item_id
            WHERE s.project_id = ?
            ORDER BY s.created_at ASC
            """,
            (project_id,)
        )
        return [dict(r) for r in rows]

    def export_credits_text(self, project_id: str) -> str:
        """Generate human-readable credits.txt formatted with attribution and source links."""
        sources = self.get_project_provenance(project_id)
        if not sources:
            return "В проекте используются только сгенерированные и локальные материалы пользователя.\n"

        lines = [
            "=" * 70,
            "ТИТРЫ И АВТОРСКИЕ ПРАВА НА ИСПОЛЬЗОВАННЫЕ МАТЕРИАЛЫ",
            "=" * 70,
            ""
        ]

        for idx, s in enumerate(sources, 1):
            provider = s.get("provider", "").capitalize()
            author = s.get("author_name") or "Не указан"
            lic = s.get("license_type") or "Лицензия не указана"
            page_url = s.get("source_page_url") or ""
            author_url = s.get("author_url") or ""
            name = s.get("original_name") or f"Файл #{s.get('item_id', '')[:8]}"

            lines.append(f"{idx}. {name}")
            lines.append(f"   Источник: {provider}")
            lines.append(f"   Автор: {author}")
            if author_url:
                lines.append(f"   Профиль автора: {author_url}")
            lines.append(f"   Лицензия / Условия: {lic}")
            if page_url:
                lines.append(f"   Страница оригинала: {page_url}")
            lines.append("")

        lines.append("=" * 70)
        lines.append("Сформировано автоматически FrameForge Studio 2.0")
        lines.append("=" * 70)
        return "\n".join(lines)

    def export_credits_json(self, project_id: str) -> Dict[str, Any]:
        """Generate structured JSON attribution dictionary for metadata export."""
        sources = self.get_project_provenance(project_id)
        return {
            "project_id": project_id,
            "total_attributed_materials": len(sources),
            "materials": sources
        }
