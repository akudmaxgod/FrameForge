"""SQLite repositories. Transactions never span provider calls."""
import json
import sqlite3
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS editorial_scene_plans(
 id TEXT PRIMARY KEY, scene_id TEXT NOT NULL REFERENCES scenes(id) ON DELETE CASCADE, project_id TEXT NOT NULL REFERENCES projects(id),
 revision INTEGER NOT NULL, content_hash TEXT NOT NULL, document TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(scene_id,revision), UNIQUE(scene_id,content_hash));
CREATE TABLE IF NOT EXISTS editorial_scene_applications(
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES editorial_scene_plans(id) ON DELETE CASCADE,
 scene_id TEXT NOT NULL REFERENCES scenes(id) ON DELETE CASCADE, source_revision INTEGER NOT NULL, target_revision INTEGER NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(plan_id));
CREATE TABLE IF NOT EXISTS voice_plan_versions(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), revision INTEGER NOT NULL,
 content_hash TEXT NOT NULL, document TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(project_id,revision), UNIQUE(project_id,content_hash));
CREATE TABLE IF NOT EXISTS voice_plan_applications(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 plan_id TEXT NOT NULL REFERENCES voice_plan_versions(id), base_snapshot TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS voice_workflows(
 project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
 audio_asset_id TEXT NOT NULL, story_sha256 TEXT NOT NULL,
 state TEXT NOT NULL, stage TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
 options_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}',
 pause_requested INTEGER NOT NULL DEFAULT 0, error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_edits(
 project_id TEXT PRIMARY KEY REFERENCES projects(id), revision INTEGER NOT NULL,
 schema_version INTEGER NOT NULL, document_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS edit_exports(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), edit_revision INTEGER NOT NULL,
 snapshot TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
 stage TEXT NOT NULL DEFAULT '', error TEXT, asset_id TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS edit_export_active ON edit_exports(project_id,edit_revision)
 WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS projects(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, idea TEXT NOT NULL,
 story TEXT NOT NULL, style TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'characters', world TEXT NOT NULL DEFAULT '',
 camera_mode TEXT NOT NULL DEFAULT 'cinema', camera_pacing TEXT NOT NULL DEFAULT 'steady', aspect_ratio TEXT NOT NULL DEFAULT '16:9',
  cast_bible TEXT NOT NULL DEFAULT '{}', animation_settings TEXT NOT NULL DEFAULT '{}',
  analysis_metadata TEXT NOT NULL DEFAULT '{}',
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_trash(
 project_id TEXT PRIMARY KEY REFERENCES projects(id), deleted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_library_items(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 file_path TEXT NOT NULL, original_name TEXT NOT NULL, media_type TEXT NOT NULL,
 sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, duration_ms REAL NOT NULL DEFAULT 0,
 width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0, fps REAL NOT NULL DEFAULT 0,
 thumbnail_path TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_media_items_project ON media_library_items(project_id);
CREATE INDEX IF NOT EXISTS idx_media_items_sha256 ON media_library_items(sha256);
CREATE TABLE IF NOT EXISTS media_segments(
 id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES media_library_items(id) ON DELETE CASCADE,
 project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 segment_index INTEGER NOT NULL DEFAULT 0, start_ms REAL NOT NULL DEFAULT 0, end_ms REAL NOT NULL DEFAULT 0,
 duration_ms REAL NOT NULL DEFAULT 0, keyframe_paths_json TEXT NOT NULL DEFAULT '[]',
 analysis_summary TEXT NOT NULL DEFAULT '', dominant_subject TEXT NOT NULL DEFAULT '',
 scene_type TEXT NOT NULL DEFAULT 'b-roll', setting TEXT NOT NULL DEFAULT 'interior',
 period_epoch TEXT NOT NULL DEFAULT '', mood TEXT NOT NULL DEFAULT '',
 objects_json TEXT NOT NULL DEFAULT '[]', actions_json TEXT NOT NULL DEFAULT '[]',
 people_count INTEGER NOT NULL DEFAULT 0, visual_defects_json TEXT NOT NULL DEFAULT '[]',
 recommended_montage_roles_json TEXT NOT NULL DEFAULT '[]',
 analysis_status TEXT NOT NULL DEFAULT 'pending', analysis_error TEXT,
 analysis_model TEXT NOT NULL DEFAULT '', analysis_version INTEGER NOT NULL DEFAULT 1,
 content_hash TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_media_segments_item ON media_segments(item_id);
CREATE INDEX IF NOT EXISTS idx_media_segments_project ON media_segments(project_id);
CREATE INDEX IF NOT EXISTS idx_media_segments_status ON media_segments(analysis_status);
CREATE INDEX IF NOT EXISTS idx_media_segments_hash ON media_segments(content_hash);
CREATE TABLE IF NOT EXISTS media_analysis_jobs(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'queued', progress REAL NOT NULL DEFAULT 0.0,
 total_segments INTEGER NOT NULL DEFAULT 0, processed_segments INTEGER NOT NULL DEFAULT 0,
 current_step TEXT NOT NULL DEFAULT '', error TEXT, model TEXT NOT NULL DEFAULT 'qwen3-vl-4b',
 cancel_requested INTEGER NOT NULL DEFAULT 0, started_at TEXT, finished_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_media_jobs_project ON media_analysis_jobs(project_id, state);
CREATE TABLE IF NOT EXISTS media_match_proposals(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 base_edit_revision INTEGER NOT NULL, voice_binding_hash TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'proposed', proposals_json TEXT NOT NULL DEFAULT '[]',
 target_document_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_media_proposals_project ON media_match_proposals(project_id);
CREATE TABLE IF NOT EXISTS stock_search_cache(
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, query_key TEXT NOT NULL,
 media_type TEXT NOT NULL, response_json TEXT NOT NULL, total_hits INTEGER NOT NULL DEFAULT 0,
 fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL, UNIQUE(provider, query_key));
CREATE INDEX IF NOT EXISTS idx_stock_cache_lookup ON stock_search_cache(provider, query_key, expires_at);
CREATE TABLE IF NOT EXISTS stock_results(
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL,
 media_type TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
 page_url TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '', author_url TEXT NOT NULL DEFAULT '',
 duration_sec REAL NOT NULL DEFAULT 0, width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
 preview_url TEXT NOT NULL DEFAULT '', large_url TEXT NOT NULL DEFAULT '', download_url TEXT NOT NULL DEFAULT '',
 metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(provider, external_id));
CREATE INDEX IF NOT EXISTS idx_stock_results_lookup ON stock_results(provider, external_id);
CREATE TABLE IF NOT EXISTS material_search_plans(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 snapshot_hash TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
 options_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_material_plans_project ON material_search_plans(project_id);
CREATE TABLE IF NOT EXISTS material_search_requirements(
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES material_search_plans(id) ON DELETE CASCADE,
 segment_key TEXT NOT NULL, origin_clip_ids_json TEXT NOT NULL DEFAULT '[]',
 narration_origin_clip_id TEXT NOT NULL DEFAULT '', quote TEXT NOT NULL DEFAULT '',
 start_ms INTEGER, end_ms INTEGER, duration_ms INTEGER NOT NULL DEFAULT 0,
 subject_entities_json TEXT NOT NULL DEFAULT '[]', action TEXT NOT NULL DEFAULT '',
 location TEXT NOT NULL DEFAULT '', period TEXT NOT NULL DEFAULT '',
 mandatory_details_json TEXT NOT NULL DEFAULT '[]', forbidden_substitutions_json TEXT NOT NULL DEFAULT '[]',
 universe_id TEXT NOT NULL DEFAULT '', requested_role TEXT NOT NULL DEFAULT 'exact',
 generated_queries_json TEXT NOT NULL DEFAULT '[]', user_queries_json TEXT NOT NULL DEFAULT '[]',
 enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, UNIQUE(plan_id, segment_key));
CREATE INDEX IF NOT EXISTS idx_material_reqs_plan ON material_search_requirements(plan_id);
CREATE TABLE IF NOT EXISTS material_search_jobs(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 plan_id TEXT NOT NULL REFERENCES material_search_plans(id) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'searching',
 progress REAL NOT NULL DEFAULT 0.0, total_queries INTEGER NOT NULL DEFAULT 0,
 processed_queries INTEGER NOT NULL DEFAULT 0, total_candidates INTEGER NOT NULL DEFAULT 0,
 verified_candidates INTEGER NOT NULL DEFAULT 0, current_step TEXT NOT NULL DEFAULT '',
 checkpoint_json TEXT NOT NULL DEFAULT '{}', error_message TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_material_jobs_project ON material_search_jobs(project_id, state);
CREATE TABLE IF NOT EXISTS material_search_candidates(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES material_search_jobs(id) ON DELETE CASCADE,
 requirement_id TEXT NOT NULL REFERENCES material_search_requirements(id) ON DELETE CASCADE,
 result_id TEXT NOT NULL, provider TEXT NOT NULL, external_id TEXT NOT NULL,
 media_type TEXT NOT NULL DEFAULT 'photo', candidate_json TEXT NOT NULL DEFAULT '{}',
 semantic_status TEXT NOT NULL DEFAULT 'needs_review', identity_status TEXT NOT NULL DEFAULT 'not_required',
 rights_status TEXT NOT NULL DEFAULT 'unknown', reasons_json TEXT NOT NULL DEFAULT '[]',
 rank_score REAL NOT NULL DEFAULT 0.0, is_selected INTEGER NOT NULL DEFAULT 0,
 is_imported INTEGER NOT NULL DEFAULT 0, imported_item_id TEXT, created_at TEXT NOT NULL,
 UNIQUE(job_id, requirement_id, provider, external_id));
CREATE INDEX IF NOT EXISTS idx_material_candidates_job ON material_search_candidates(job_id, requirement_id);
CREATE TABLE IF NOT EXISTS material_entity_catalog(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 entity_key TEXT NOT NULL, canonical_name TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]',
 universe_id TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'character',
 references_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(project_id, entity_key));
CREATE INDEX IF NOT EXISTS idx_material_catalog_project ON material_entity_catalog(project_id);
CREATE TABLE IF NOT EXISTS material_qwen_verification_cache(
 cache_key TEXT PRIMARY KEY,
 status TEXT NOT NULL,
 status_label TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_material_qwen_cache_key ON material_qwen_verification_cache(cache_key);
CREATE TABLE IF NOT EXISTS stock_rate_state(
 provider TEXT PRIMARY KEY, rate_limit INTEGER NOT NULL DEFAULT 100,
 rate_remaining INTEGER NOT NULL DEFAULT 100, rate_reset_at REAL NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_library_sources(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 item_id TEXT NOT NULL REFERENCES media_library_items(id) ON DELETE CASCADE,
 provider TEXT NOT NULL, external_id TEXT NOT NULL, source_page_url TEXT NOT NULL DEFAULT '',
 author_name TEXT NOT NULL DEFAULT '', author_url TEXT NOT NULL DEFAULT '',
 license_type TEXT NOT NULL DEFAULT 'free_pixabay', created_at TEXT NOT NULL, UNIQUE(project_id, item_id));
CREATE INDEX IF NOT EXISTS idx_media_sources_item ON media_library_sources(project_id, item_id);
CREATE TABLE IF NOT EXISTS material_browse_sessions(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 provider TEXT NOT NULL, query TEXT NOT NULL, filters_json TEXT NOT NULL DEFAULT '{}',
 mode TEXT NOT NULL DEFAULT 'manual', state TEXT NOT NULL DEFAULT 'completed',
 error_message TEXT, total_results INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_material_browse_sessions_project ON material_browse_sessions(project_id);
CREATE TABLE IF NOT EXISTS material_browse_items(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES material_browse_sessions(id) ON DELETE CASCADE,
 project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 provider TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
 description TEXT NOT NULL DEFAULT '', source_page_url TEXT NOT NULL DEFAULT '',
 preview_url TEXT NOT NULL DEFAULT '', thumbnail_url TEXT NOT NULL DEFAULT '',
 large_url TEXT NOT NULL DEFAULT '', download_url TEXT NOT NULL DEFAULT '',
 width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
 format TEXT NOT NULL DEFAULT 'jpeg', license_name TEXT NOT NULL DEFAULT '',
 rights_status TEXT NOT NULL DEFAULT 'unknown', author TEXT NOT NULL DEFAULT '',
 metadata_json TEXT NOT NULL DEFAULT '{}', is_imported INTEGER NOT NULL DEFAULT 0,
 imported_item_id TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_material_browse_items_session ON material_browse_items(session_id);
CREATE TABLE IF NOT EXISTS material_search_usage(
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, period TEXT NOT NULL,
 requests_count INTEGER NOT NULL DEFAULT 0, configured_limit INTEGER NOT NULL DEFAULT 50,
 allow_paid INTEGER NOT NULL DEFAULT 0, last_request_at TEXT NOT NULL,
 UNIQUE(provider, period));
CREATE INDEX IF NOT EXISTS idx_material_usage_provider ON material_search_usage(provider, period);
CREATE TABLE IF NOT EXISTS characters(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL, description TEXT NOT NULL, wardrobe TEXT NOT NULL DEFAULT '{}',
 cast_id TEXT NOT NULL DEFAULT '',
 canonical_name TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '[]',
 role TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
 evidence TEXT NOT NULL DEFAULT '[]',
 revision INTEGER NOT NULL DEFAULT 1, locked INTEGER NOT NULL DEFAULT 0,
 references_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS locations(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL, description TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, locked INTEGER NOT NULL DEFAULT 0,
 references_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS props(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL, description TEXT NOT NULL,
 importance TEXT NOT NULL DEFAULT 'standard',
 first_appearance INTEGER NOT NULL DEFAULT 1,
 owner TEXT NOT NULL DEFAULT '',
 default_state TEXT NOT NULL DEFAULT 'absent',
 revision INTEGER NOT NULL DEFAULT 1, locked INTEGER NOT NULL DEFAULT 0,
 references_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS scenes(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 position INTEGER NOT NULL, current_revision INTEGER NOT NULL DEFAULT 1,
 active_generation_id TEXT, video_asset_id TEXT REFERENCES assets(id), UNIQUE(project_id,position));
CREATE TABLE IF NOT EXISTS scene_revisions(
 scene_id TEXT NOT NULL REFERENCES scenes(id), revision INTEGER NOT NULL,
 data TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(scene_id,revision));
CREATE TABLE IF NOT EXISTS batches(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(project_id,idempotency_key));
CREATE TABLE IF NOT EXISTS generations(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 scene_id TEXT REFERENCES scenes(id), scene_revision INTEGER,
 character_id TEXT REFERENCES characters(id), character_revision INTEGER,
 location_id TEXT REFERENCES locations(id), location_revision INTEGER,
 prop_id TEXT REFERENCES props(id), prop_revision INTEGER,
 purpose TEXT NOT NULL, batch_id TEXT REFERENCES batches(id),
 state TEXT NOT NULL, snapshot TEXT NOT NULL, seed INTEGER NOT NULL,
 provider_id TEXT, asset_id TEXT, error TEXT, progress TEXT NOT NULL DEFAULT '{}',
 attempts INTEGER NOT NULL DEFAULT 0, started_at TEXT, finished_at TEXT,
 created_at TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS generations_queue ON generations(state,created_at);
CREATE TABLE IF NOT EXISTS reference_casts(
 target_kind TEXT NOT NULL, target_id TEXT NOT NULL, revision INTEGER NOT NULL,
 project_id TEXT NOT NULL REFERENCES projects(id), generation_ids TEXT NOT NULL,
 selected_asset_id TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(target_kind,target_id,revision));
CREATE TABLE IF NOT EXISTS scene_fidelity_runs(
 scene_id TEXT NOT NULL, context_hash TEXT NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(scene_id,context_hash));
CREATE TABLE IF NOT EXISTS story_audits(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 chunk_key TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS story_audits_project ON story_audits(project_id,created_at);
CREATE TABLE IF NOT EXISTS assets(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 generation_id TEXT NOT NULL REFERENCES generations(id),
 path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, width INTEGER NOT NULL,
 height INTEGER NOT NULL, bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(
 generation_id TEXT PRIMARY KEY REFERENCES generations(id),
 accepted INTEGER NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending_review', reason_code TEXT NOT NULL DEFAULT '',
 continuity_json TEXT NOT NULL DEFAULT '{}', passport_json TEXT NOT NULL DEFAULT '{}',
 updated_at TEXT);
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT,
 kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS veo_jobs(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 scene_id TEXT NOT NULL REFERENCES scenes(id),
 idempotency_key TEXT NOT NULL,
 model TEXT NOT NULL DEFAULT 'veo-3.1-lite',
 engine TEXT,
 source_image_asset_id TEXT,
 source_image_sha256 TEXT,
 source_generation_id TEXT,
 source_revision INTEGER,
 duration_seconds INTEGER,
 resolution TEXT,
 prompt TEXT,
 camera_motion TEXT,
 status TEXT NOT NULL DEFAULT 'queued_veo',
 error TEXT,
 generation_id TEXT REFERENCES generations(id),
 asset_id TEXT REFERENCES assets(id),
 flow_media_id TEXT,
 created_at TEXT NOT NULL,
 completed_at TEXT,
 UNIQUE(scene_id, idempotency_key));
CREATE INDEX IF NOT EXISTS idx_veo_jobs_scene ON veo_jobs(scene_id);
CREATE TABLE IF NOT EXISTS channel_profiles(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT, description TEXT NOT NULL DEFAULT '',
 profile_type TEXT NOT NULL DEFAULT 'stickman_story', is_archived INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, active_version_id TEXT);
CREATE INDEX IF NOT EXISTS idx_channel_profiles_archived ON channel_profiles(is_archived);
CREATE TABLE IF NOT EXISTS channel_profile_versions(
 id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channel_profiles(id),
 version_number INTEGER NOT NULL, style_prompt TEXT NOT NULL DEFAULT '',
 negative_prompt TEXT NOT NULL DEFAULT '', composition_rules TEXT NOT NULL DEFAULT '',
 color_palette TEXT NOT NULL DEFAULT '', line_style TEXT NOT NULL DEFAULT '',
 background_style TEXT NOT NULL DEFAULT '', aspect_ratio TEXT NOT NULL DEFAULT '16:9',
 animation_rules TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 UNIQUE(channel_id, version_number));
CREATE INDEX IF NOT EXISTS idx_channel_versions_channel ON channel_profile_versions(channel_id);
CREATE TABLE IF NOT EXISTS channel_characters(
 id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channel_profiles(id),
 canonical_name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'main',
 description TEXT NOT NULL DEFAULT '', appearance_prompt TEXT NOT NULL DEFAULT '',
 negative_prompt TEXT NOT NULL DEFAULT '', is_locked INTEGER NOT NULL DEFAULT 0,
 is_active INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS idx_channel_characters_channel ON channel_characters(channel_id);
CREATE TABLE IF NOT EXISTS channel_character_versions(
 id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES channel_characters(id),
 version_number INTEGER NOT NULL, appearance_prompt TEXT NOT NULL DEFAULT '',
 wardrobe_rules TEXT NOT NULL DEFAULT '', proportion_rules TEXT NOT NULL DEFAULT '',
 face_rules TEXT NOT NULL DEFAULT '', color_rules TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, UNIQUE(character_id, version_number));
CREATE INDEX IF NOT EXISTS idx_channel_char_versions_char ON channel_character_versions(character_id);
CREATE TABLE IF NOT EXISTS channel_reference_assets(
 id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channel_profiles(id),
 character_id TEXT REFERENCES channel_characters(id), asset_type TEXT NOT NULL,
 file_path TEXT NOT NULL, content_hash TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
 is_primary INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_channel_ref_assets_channel ON channel_reference_assets(channel_id, character_id);
"""


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.executescript(SCHEMA)
            connection.execute("""CREATE TABLE IF NOT EXISTS veo_jobs(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                scene_id TEXT NOT NULL REFERENCES scenes(id),
                idempotency_key TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT 'veo-3.1-lite',
                engine TEXT,
                source_image_asset_id TEXT,
                source_image_sha256 TEXT,
                source_generation_id TEXT,
                source_revision INTEGER,
                duration_seconds INTEGER,
                resolution TEXT,
                prompt TEXT,
                camera_motion TEXT,
                status TEXT NOT NULL DEFAULT 'queued_veo',
                error TEXT,
                generation_id TEXT REFERENCES generations(id),
                asset_id TEXT REFERENCES assets(id),
                flow_media_id TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(scene_id, idempotency_key))""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_veo_jobs_scene ON veo_jobs(scene_id)")
            veo_jobs_columns = {row[1] for row in connection.execute('PRAGMA table_info(veo_jobs)')}
            for col, col_type in [
                ('engine', 'TEXT'),
                ('source_image_asset_id', 'TEXT'),
                ('source_image_sha256', 'TEXT'),
                ('source_generation_id', 'TEXT'),
                ('source_revision', 'INTEGER'),
                ('duration_seconds', 'INTEGER'),
                ('resolution', 'TEXT'),
                ('prompt', 'TEXT'),
                ('camera_motion', 'TEXT'),
                ('error', 'TEXT'),
            ]:
                if col not in veo_jobs_columns:
                    connection.execute(f'ALTER TABLE veo_jobs ADD COLUMN {col} {col_type}')
            connection.execute("""CREATE TABLE IF NOT EXISTS props(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                name TEXT NOT NULL, description TEXT NOT NULL,
                importance TEXT NOT NULL DEFAULT 'standard',
                first_appearance INTEGER NOT NULL DEFAULT 1,
                owner TEXT NOT NULL DEFAULT '',
                default_state TEXT NOT NULL DEFAULT 'absent',
                revision INTEGER NOT NULL DEFAULT 1, locked INTEGER NOT NULL DEFAULT 0,
                references_json TEXT NOT NULL DEFAULT '{}')""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_props_project ON props(project_id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS channel_profiles(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                profile_type TEXT NOT NULL DEFAULT 'stickman_story', is_archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, active_version_id TEXT)""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_profiles_archived ON channel_profiles(is_archived)")
            connection.execute("""CREATE TABLE IF NOT EXISTS channel_profile_versions(
                id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channel_profiles(id),
                version_number INTEGER NOT NULL, style_prompt TEXT NOT NULL DEFAULT '',
                negative_prompt TEXT NOT NULL DEFAULT '', composition_rules TEXT NOT NULL DEFAULT '',
                color_palette TEXT NOT NULL DEFAULT '', line_style TEXT NOT NULL DEFAULT '',
                background_style TEXT NOT NULL DEFAULT '', aspect_ratio TEXT NOT NULL DEFAULT '16:9',
                animation_rules TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                UNIQUE(channel_id, version_number))""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_versions_channel ON channel_profile_versions(channel_id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS channel_characters(
                id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channel_profiles(id),
                canonical_name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'main',
                description TEXT NOT NULL DEFAULT '', appearance_prompt TEXT NOT NULL DEFAULT '',
                negative_prompt TEXT NOT NULL DEFAULT '', is_locked INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1)""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_characters_channel ON channel_characters(channel_id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS channel_character_versions(
                id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES channel_characters(id),
                version_number INTEGER NOT NULL, appearance_prompt TEXT NOT NULL DEFAULT '',
                wardrobe_rules TEXT NOT NULL DEFAULT '', proportion_rules TEXT NOT NULL DEFAULT '',
                face_rules TEXT NOT NULL DEFAULT '', color_rules TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, UNIQUE(character_id, version_number))""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_char_versions_char ON channel_character_versions(character_id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS channel_reference_assets(
                id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channel_profiles(id),
                character_id TEXT REFERENCES channel_characters(id), asset_type TEXT NOT NULL,
                file_path TEXT NOT NULL, content_hash TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
                is_primary INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_ref_assets_channel ON channel_reference_assets(channel_id, character_id)")
            columns = {row[1] for row in connection.execute('PRAGMA table_info(projects)')}
            if 'mode' not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN mode TEXT NOT NULL DEFAULT 'characters'")
            if 'camera_custom' not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN camera_custom TEXT NOT NULL DEFAULT ''")
            if 'camera_mode' not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN camera_mode TEXT NOT NULL DEFAULT 'cinema'")
            if 'camera_pacing' not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN camera_pacing TEXT NOT NULL DEFAULT 'steady'")
            if 'aspect_ratio' not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN aspect_ratio TEXT NOT NULL DEFAULT '16:9'")
            if 'cast_bible' not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN cast_bible TEXT NOT NULL DEFAULT '{}'")
            if 'animation_settings' not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN animation_settings TEXT NOT NULL DEFAULT '{}'")
            if 'analysis_metadata' not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN analysis_metadata TEXT NOT NULL DEFAULT '{}'")
            if 'channel_profile_id' not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN channel_profile_id TEXT REFERENCES channel_profiles(id)")
            if 'channel_profile_version_id' not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN channel_profile_version_id TEXT REFERENCES channel_profile_versions(id)")
            if 'channel_character_version_map' not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN channel_character_version_map TEXT NOT NULL DEFAULT '{}'")
            cp_columns = {row[1] for row in connection.execute('PRAGMA table_info(channel_profiles)')}
            if 'slug' not in cp_columns:
                connection.execute("ALTER TABLE channel_profiles ADD COLUMN slug TEXT")
            char_columns = {row[1] for row in connection.execute('PRAGMA table_info(characters)')}
            for col, col_type, default in [
                ('cast_id', 'TEXT', "''"),
                ('canonical_name', 'TEXT', "''"),
                ('aliases', 'TEXT', "'[]'"),
                ('role', 'TEXT', "''"),
                ('status', 'TEXT', "'active'"),
                ('evidence', 'TEXT', "'[]'"),
            ]:
                if col not in char_columns:
                    connection.execute(f"ALTER TABLE characters ADD COLUMN {col} {col_type} NOT NULL DEFAULT {default}")
            connection.execute("UPDATE characters SET cast_id = id WHERE cast_id = ''")
            generation_columns = {row[1] for row in connection.execute('PRAGMA table_info(generations)')}
            if 'location_id' not in generation_columns:
                connection.execute('ALTER TABLE generations ADD COLUMN location_id TEXT REFERENCES locations(id)')
            if 'location_revision' not in generation_columns:
                connection.execute('ALTER TABLE generations ADD COLUMN location_revision INTEGER')
            if 'prop_id' not in generation_columns:
                connection.execute('ALTER TABLE generations ADD COLUMN prop_id TEXT REFERENCES props(id)')
            if 'prop_revision' not in generation_columns:
                connection.execute('ALTER TABLE generations ADD COLUMN prop_revision INTEGER')
            scene_columns = {row[1] for row in connection.execute('PRAGMA table_info(scenes)')}
            if 'video_asset_id' not in scene_columns:
                connection.execute('ALTER TABLE scenes ADD COLUMN video_asset_id TEXT REFERENCES assets(id)')
            review_columns = {row[1] for row in connection.execute('PRAGMA table_info(reviews)')}
            for col, col_type, default in [
                ('status', 'TEXT', "'pending_review'"),
                ('reason_code', 'TEXT', "''"),
                ('continuity_json', 'TEXT', "'{}'"),
                ('passport_json', 'TEXT', "'{}'"),
                ('updated_at', 'TEXT', 'NULL'),
            ]:
                if col not in review_columns:
                    connection.execute(f'ALTER TABLE reviews ADD COLUMN {col} {col_type} DEFAULT {default}')
            legacy_pending = " OR status='pending_review'" if 'status' not in review_columns else ''
            connection.execute("UPDATE reviews SET status=CASE WHEN accepted=1 THEN 'approved' ELSE 'rejected' END WHERE status IS NULL OR status=''" + legacy_pending)
            connection.execute("UPDATE reviews SET updated_at=created_at WHERE updated_at IS NULL")
            version = connection.execute('SELECT max(version) FROM schema_version').fetchone()[0]
            if version != 1:
                raise RuntimeError(f'Unsupported database schema: {version}')
            # closing() closes without committing. Earlier UPDATE statements
            # start a transaction, so subsequent legacy ALTER TABLE migrations
            # would otherwise be rolled back when the connection is closed.
            connection.commit()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA busy_timeout=5000')
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def all(self, sql, parameters=()):
        connection = self.connect()
        try:
            return [dict(row) for row in connection.execute(sql, parameters)]
        finally:
            connection.close()

    def one(self, sql, parameters=()):
        rows = self.all(sql, parameters)
        return rows[0] if rows else None

    @staticmethod
    def event(connection, project_id, kind, data):
        connection.execute('INSERT INTO events(project_id,kind,data,created_at) VALUES(?,?,?,?)',
                           (project_id, kind, dump(data), now()))
