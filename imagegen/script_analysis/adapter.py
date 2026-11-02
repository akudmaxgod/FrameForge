"""Adapter from DeepSeek AnalysisResult / Registry + Scenes to FrameForge canonical analysis schema."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from dataclasses import asdict

from .schemas import Registry, Scene
from .analyzer import AnalysisResult


def _find_heading_in_story(heading: str, story: str, start_search: int = 0) -> int:
    """Finds position of scene heading in story, with fuzzy normalization fallback."""
    if not heading or not story:
        return -1
    # 1. Exact match
    pos = story.find(heading, start_search)
    if pos >= 0:
        return pos
    # 2. Case-insensitive match
    lower_story = story.lower()
    lower_heading = heading.lower()
    pos = lower_story.find(lower_heading, start_search)
    if pos >= 0:
        return pos
    return -1


def export_to_app_analysis(
    result: AnalysisResult,
    story_text: str,
    project: Optional[Dict[str, Any]] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
    mode: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Converts DeepSeek AnalysisResult into FrameForge canonical project analysis schema."""
    registry = result.registry
    scenes = result.scenes
    warnings = list(result.warnings)

    # 1. Characters
    characters: List[Dict[str, Any]] = []
    char_by_id: Dict[str, Any] = {}
    for c in registry.characters:
        aliases = list(dict.fromkeys([c.name, *c.aliases]))
        char_dict = {
            "name": c.name,
            "description": c.description or f"Персонаж {c.name}.",
            "wardrobe": {"default": f"Базовый образ персонажа {c.name}"},
            "cast_id": c.id,
            "canonical_name": c.name,
            "aliases": aliases,
            "entity_type": getattr(c, "entity_type", "unknown"),
            "identity_scope": getattr(c, "identity_scope", "unspecified"),
            "role": c.role if c.role in ("protagonist", "antagonist", "supporting", "minor") else "supporting",
            "arc": c.arc,
            "status": "active",
            "evidence": [],
        }
        characters.append(char_dict)
        char_by_id[c.id] = c

    # 2. Locations
    locations: List[Dict[str, Any]] = []
    loc_by_id: Dict[str, Any] = {}
    for l in registry.locations:
        loc_dict = {
            "id": l.id,
            "name": l.name,
            "description": l.description or f"Локация {l.name}.",
            "int_ext": l.int_ext,
        }
        locations.append(loc_dict)
        loc_by_id[l.id] = l

    # 3. Props
    props: List[Dict[str, Any]] = []
    for p in registry.props:
        props.append({
            "id": p.id,
            "name": p.name,
            "description": p.importance or p.name,
            "importance": "story",
            "first_appearance": 1,
            "owner": "",
            "default_state": "absent",
        })

    # 4. Spans & Partitioning of story text
    story_len = len(story_text)
    heading_positions: List[int] = []
    curr_pos = 0
    for s in scenes:
        pos = _find_heading_in_story(s.heading, story_text, curr_pos)
        if pos >= 0 and (not heading_positions or pos >= heading_positions[-1]):
            heading_positions.append(pos)
            curr_pos = pos + len(s.heading)
        else:
            heading_positions.append(-1)

    from .alignment import align_scenes
    resolved_starts, resolved_ends, alignment_warnings = align_scenes(scenes, story_text, heading_positions)
    warnings.extend(alignment_warnings)

    # 5. Build canonical scenes
    canonical_scenes: List[Dict[str, Any]] = []
    for i, s in enumerate(scenes):
        s_start = resolved_starts[i] if i < len(resolved_starts) else 0
        s_end = resolved_ends[i] if i < len(resolved_ends) else story_len
        quote = story_text[s_start:s_end]

        loc_entry = loc_by_id.get(s.location_id)
        loc_name = loc_entry.name if loc_entry else ""
        loc_id = loc_entry.id if loc_entry else ""

        # Extract location candidate from heading
        heading_clean = s.heading.strip()
        heading_loc = heading_clean
        for prefix in ("INT. ", "EXT. ", "INT./EXT. ", "I/E. "):
            if heading_loc.upper().startswith(prefix):
                heading_loc = heading_loc[len(prefix):]
                break
        if " - " in heading_loc:
            heading_loc = heading_loc.split(" - ")[0].strip()
        heading_loc_title = heading_loc.capitalize() if heading_loc.isupper() else heading_loc

        scene_char_names: List[str] = []
        for cid in s.character_ids:
            if cid in char_by_id:
                cname = char_by_id[cid].name
                if cname not in scene_char_names:
                    scene_char_names.append(cname)

        # Check for cross-character location conflict (e.g. Eliza's scene assigned Anna's dressing table):
        # A character's personal room must not be inherited by an unrelated person's scene.
        other_char_in_loc = False
        if loc_name:
            for c in registry.characters:
                c_stem = c.name[:max(3, len(c.name)-1)].casefold()
                if c_stem in loc_name.casefold() and c.name not in scene_char_names:
                    other_char_in_loc = True
                    break

        if other_char_in_loc and heading_loc_title:
            loc_name = heading_loc_title
            loc_id = ""
            existing_loc = next((l for l in locations if l["name"].casefold() == loc_name.casefold()), None)
            if existing_loc:
                loc_id = existing_loc["id"]
            else:
                new_loc_id = f"loc_{len(locations) + 1}"
                new_loc = {
                    "id": new_loc_id,
                    "name": loc_name,
                    "description": f"Локация {loc_name}.",
                    "int_ext": s.heading[:3].lower() if s.heading.startswith(("INT", "EXT")) else "int",
                }
                locations.append(new_loc)
                loc_id = new_loc_id

        # Generic settings do not establish shared physical identity. Scope by
        # explicit chapter markers; with no chapter evidence keep scenes separate.
        if loc_name and re.search(r"кабинет|комнат|салон|office|room|salon", loc_name, re.I) and not other_char_in_loc:
            chapters = list(re.finditer(r"(?im)^\s*(?:на\s+[^\n.!?]{1,35}\s+месте|глава\s+[^\n]{1,60}|chapter\s+[^\n]{1,60}|история\s+\d+)[^\n]*", story_text))
            episode = sum(m.start() <= s_start for m in chapters)
            scope = f"эпизод {episode}" if chapters else f"сцена {i+1}"
            scoped_id = f"{s.location_id}_scope_{episode if chapters else i+1}"
            loc_name = f"{loc_name} · {scope}"
            loc_id = scoped_id
            if not any(l['id'] == scoped_id for l in locations):
                locations.append({'id':scoped_id,'name':loc_name,'description':loc_entry.description if loc_entry else loc_name,'int_ext':loc_entry.int_ext if loc_entry else 'INT'})

        if not loc_name:
            loc_name = heading_loc_title or "Обстановка сцены"

        # Separate physical participant, depicted representation (statue, sculpture, portrait),
        # and historical/author mentions.
        # DeepSeek's explicit character_ids are respected; absence of full name in summary
        # does not demote characters referred to by role, surname or pronouns.
        physical, depicted, mentioned = [], [], []
        full_scene_text = f"{s.heading}. {s.summary}. {quote}".casefold()

        for name in scene_char_names:
            c = next(c for c in registry.characters if c.name == name)
            aliases = [c.name, *c.aliases]
            stems = []
            for a in aliases:
                if not a:
                    continue
                for w in a.split():
                    if len(w) >= 3:
                        stems.append(w[:max(3, len(w)-2)].casefold())

            attribution = any(re.search(
                r"(?:таблет\w*|упаков\w*|короб\w*|препарат\w*|тоник\w*|этикет\w*|книг\w*|pills?|packag\w*|bottle|book)" +
                r"[^.!?;]{0,90}(?:доктор\w*\s+)?" + re.escape(st), s.summary.casefold()) for st in stems)
            if attribution:
                mentioned.append(name)
                warnings.append(f"Сцена {i+1}: {name} — атрибуция предмета; не добавлен как живой участник.")
                continue

            # 1. Depicted check: statue, sculpture, bust, portrait syntactically attached to this character
            is_depicted = False
            for st in stems:
                pat_dep = re.compile(
                    r'(?:стату[яеиейю]|скульптур[аеыуой]|скульптурн\w*|бюст[аеу]?|портрет[аеу]?|изображени[еяю]|'
                    r'sculptur\w*|statue\s+of|bust\s+of|portrait\s+of)\s+(?:[а-яa-z]+\s+){0,3}' + re.escape(st),
                    re.I
                )
                pat_dep_post = re.compile(
                    re.escape(st) + r'\w*\s+(?:в\s+виде\s+(?:статуи|скульптуры|бюста|портрета)|на\s+портрете|на\s+картине|на\s+экране)',
                    re.I
                )
                if pat_dep.search(full_scene_text) or pat_dep_post.search(full_scene_text):
                    is_depicted = True
                    break

            if is_depicted:
                depicted.append(name)
                continue

            # 2. Historical or author citation check (e.g. "при Клавдии", "как писал Сенека")
            is_historical = False
            for st in stems:
                if st.startswith(('«', '"', '“')):
                    continue
                pat_hist = re.compile(
                    r'\b(?:эпох[аиу]|врем[её]н\w*|правлени[яи])\s+(?:[а-яa-z]+\s+)?' + re.escape(st) +
                    r'|\bпри\s+(?:император\w*\s+|цар\w*\s+|корол\w*\s+|княз\w*\s+|римском\s+)?' + re.escape(st),
                    re.I
                )
                pat_auth = re.compile(
                    r'(?:по\s+словам|как\s+писал|в\s+письме|в\s+трактате|сообщал\w*|рассуждал\w*|описывал\w*)\s+(?:[а-яa-z]+\s+)?' + re.escape(st),
                    re.I
                )
                if pat_hist.search(full_scene_text) or pat_auth.search(full_scene_text):
                    is_historical = True
                    break

            if is_historical:
                mentioned.append(name)
                warnings.append(f"Сцена {i+1}: {name} упомянут в историческом/авторском контексте; сохранён отдельно.")
                continue

            # 3. Otherwise: character is an active physical participant
            physical.append(name)

        scene_char_names = physical
        object_only = not physical and bool(re.search(r"^(?:демонстрируется|показан[аоы]?|крупный план|предметный|скульптур|стату|короб|упаков|displayed|close.up|a box)", s.summary.strip(), re.I))
        scene_dict = {
            "population_policy": "object_only" if object_only else "scene_participants",

            "analysis_authority": "deepseek-v2",
            "source_alignment": "exact_heading" if heading_positions and all(p >= 0 for p in heading_positions) else "estimated_paragraph_alignment",
            "depicted_people": depicted,
            "mentioned_characters": mentioned,
            "beat": s.summary,
            "heading": s.heading,
            "source_quote": quote,
            "source_start": s_start,
            "source_end": s_end,
            "location": loc_name,
            "location_ref": loc_name,
            "location_id": loc_id,
            "time": s.time_of_day,
            "shot": s.heading,
            "emotion": s.mood or "calm and attentive",
            "characters": scene_char_names,
            "character_ids": [cid for cid in s.character_ids if char_by_id[cid].name in physical],
            "visible_people": len(scene_char_names),
            "duration_sec": s.duration_sec,
            "wardrobe": {cn: "default" for cn in scene_char_names},
            "props": [],
            "negative": "",
            "continuity_before": "",
            "continuity_after": "",
        }
        from ..subject_policy import normalize_scene
        canonical_scenes.append(normalize_scene(scene_dict, characters))

    # 6. Cast bible
    cast_bible = {c["cast_id"]: c for c in characters}

    # 7. World & Diagnostics
    logline = registry.logline or ""
    genre = registry.genre or ""
    world_desc = f"{genre}: {logline}".strip(": ") if genre or logline else ""

    usage_info = {
        "tokens_in": result.stats.tokens_in,
        "tokens_out": result.stats.tokens_out,
        "cost_usd": result.stats.cost_usd,
        "prompt_cache_hit_tokens": result.stats.cache_hit_tokens,
        "cache_hit_tokens": result.stats.cache_hit_tokens,
        "prompt_cache_miss_tokens": result.stats.cache_miss_tokens,
        "cache_miss_tokens": result.stats.cache_miss_tokens,
        "result_cache_hit": result.stats.result_cache_hit,
    }

    diag = dict(diagnostics or {})
    diag["analyzer_version"] = "deepseek"
    diag["stats"] = asdict(result.stats)
    diag["usage"] = usage_info
    diag["tokens_in"] = result.stats.tokens_in
    diag["tokens_out"] = result.stats.tokens_out
    diag["cost_usd"] = result.stats.cost_usd
    diag["cache_hit"] = result.stats.cache_hit
    diag["result_cache_hit"] = result.stats.result_cache_hit
    diag["duration_ms"] = result.stats.duration_ms
    diag["scenes_count"] = len(canonical_scenes)
    diag["warnings"] = warnings
    diag["unresolved_warnings_count"] = len(warnings)

    analysis = {
        "characters": characters,
        "locations": locations,
        "props": props,
        "scenes": canonical_scenes,
        "world": world_desc,
        "cast_bible": cast_bible,
        "analyzer_version": "deepseek",
        "deepseek_usage": usage_info,
        "diagnostics": diag,
        "warnings": warnings,
    }

    # Re-run this source-backed guard on cached results too: paragraph-level
    # narration partitioning does not certify coverage of named actor events.
    from ..named_event_coverage import repair
    analysis = repair(analysis, story_text)
    analysis['diagnostics']['scenes_count'] = len(analysis['scenes'])
    analysis['diagnostics']['warnings'] = list(analysis.get('warnings', []))
    analysis['diagnostics']['unresolved_warnings_count'] = len(analysis['diagnostics']['warnings'])
    from .storyboard import plan
    return plan(analysis,story_text,kwargs.get('target'))
