"""Independent montage documents. No generation or scene writes."""
import copy
import hashlib
import io
import json
import math
import shutil
from pathlib import Path
try:
    import av
except ImportError:
    av = None
from .db import uid, now, dump
from .domain import DomainError


def number(value, low=0, high=86400000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise DomainError('Недопустимый параметр монтажа.')
    return value


def normalize(document):
    """Milliseconds, unquantized clock; black fades occupy half of each join."""
    from .montage_layers import effective_clip
    clips = [effective_clip(c) for c in document['clips'] if not c.get('excluded')]
    joins = {(t['left'], t['right']): t for t in document.get('transitions', [])}
    for c in clips:
        c['duration_ms'] = (c['trim_out_ms'] - c['trim_in_ms']) / c.get('playback_rate', 1) + c.get('hold_last_ms',0)
        c['base_duration_ms'] = c['duration_ms']
        c['in_ms'] = c['out_ms'] = 0
        c['out_kind'] = 'cut'
    for left, right in zip(clips, clips[1:]):
        t = joins.get((left['edit_clip_id'], right['edit_clip_id']), {})
        kind = t.get('kind', 'cut')
        # Reserve at most half of each clip for a join: zones never collide.
        duration = min(t.get('duration_ms', 0), left['duration_ms'] / 2, right['duration_ms'] / 2) if kind != 'cut' else 0
        left['out_ms'] = right['in_ms'] = duration
        left['out_kind'] = kind
        right['in_kind'] = kind
    cursor = 0
    for c in clips:
        c['start_ms'] = cursor
        c['end_ms'] = cursor + c['duration_ms']
        if document.get('timing_mode')=='voice_fit':
            cursor=c['end_ms']
            c['base_end_ms']=cursor
            if c['out_kind']=='dissolve':
                c['end_ms']+=c['out_ms'];c['duration_ms']+=c['out_ms']
        else:
            cursor = c['end_ms'] - (c['out_ms'] if c['out_kind'] == 'dissolve' else 0)
    return {'clips': clips, 'duration_ms': cursor, 'audio_tracks': copy.deepcopy(document.get('audio_tracks', []))}


def probe(path, kind):
    if av is None:
        raise DomainError('Для монтажа требуется PyAV с библиотеками FFmpeg. Установите зависимости приложения.', 503)
    try:
        with av.open(path if hasattr(path, 'read') else str(path)) as media:
            streams = media.streams.video if kind == 'video' else media.streams.audio
            if not streams:
                raise ValueError('missing stream')
            stream = streams[0]
            duration = float(stream.duration * stream.time_base) if stream.duration else (media.duration or 0) / av.time_base
            if duration <= 0 or duration > 86400:
                raise ValueError('duration')
            return round(duration * 1000, 3)
    except Exception as error:
        raise DomainError(f'Не удалось прочитать {kind}: {Path(str(path)).name}') from error


class Editing:
    def __init__(self, service):
        self.service, self.db = service, service.db
        self._media_cache = {}
        self._image_cache = set()

    def project(self, project_id):
        self.service.project(project_id)

    def media(self, project_id, asset_id, kind):
        asset = self.service.asset(asset_id)
        if asset['project_id'] != project_id:
            raise DomainError('Медиа не принадлежит проекту.')
        path = self.service.root / asset['path']
        stat = path.stat()
        key = (asset_id, kind, stat.st_size, stat.st_mtime_ns)
        if key not in self._media_cache:
            self._media_cache[key] = probe(path, kind)
        return self._media_cache[key]

    def get(self, project_id):
        self.project(project_id)
        row = self.db.one('SELECT * FROM project_edits WHERE project_id=?', (project_id,))
        document = json.loads(row['document_json']) if row else {'schema_version': 1, 'clips': [], 'transitions': [], 'audio_tracks': [], 'source_audio': False, 'export_settings': {'width':1920,'height':1080,'fps':24}}
        export = self.db.one('SELECT id FROM edit_exports WHERE project_id=? ORDER BY created_at DESC LIMIT 1', (project_id,))
        return {'revision': row['revision'] if row else 0, 'document': document, 'timeline': normalize(document), 'sources': self.sources(project_id, document), 'last_export_id': (export or {}).get('id')}

    def sources(self, project_id, document):
        # Match detail's scene checks without building the complete story audit,
        # generation history and media metadata on every montage autosave.
        scenes = {}
        rows = self.db.all('''SELECT s.id,s.current_revision,s.video_asset_id,
            g.id AS image_generation_id,g.asset_id AS active_image,g.scene_revision AS image_revision,
            r.data AS scene_data,old.data AS image_scene_data
            FROM scenes s LEFT JOIN generations g ON g.id=s.active_generation_id
            LEFT JOIN scene_revisions r ON r.scene_id=s.id AND r.revision=s.current_revision
            LEFT JOIN scene_revisions old ON old.scene_id=s.id AND old.revision=g.scene_revision
            WHERE s.project_id=?''', (project_id,))
        for s in rows:
            data = json.loads(s['scene_data'] or '{}')
            s['stale'] = False
            if s['image_generation_id'] and s['image_revision'] != s['current_revision']:
                if not s['image_scene_data']:
                    s['stale'] = True
                else:
                    previous = json.loads(s['image_scene_data'])
                    s['stale'] = {k:v for k,v in previous.items() if k!='animation'} != {k:v for k,v in data.items() if k!='animation'}
            video_image = data.get('animation', {}).get('source_image_asset_id')
            if s['video_asset_id'] and s['active_image'] and not video_image:
                generation = self.db.one("SELECT snapshot FROM generations WHERE asset_id=? AND purpose='scene_video'", (s['video_asset_id'],))
                try:
                    video_image = json.loads(generation['snapshot']).get('source_image_asset_id') if generation else None
                except (ValueError, TypeError):
                    video_image = None
            s['video_stale'] = bool(s['video_asset_id'] and video_image and s['active_image'] and video_image != s['active_image'])
            scenes[s['id']] = s
        result = {}
        from .montage_layers import effective_clip
        for raw in document['clips']:
            c = effective_clip(raw)
            s = scenes.get(c.get('scene_id'))
            aid=c.get('source_image_asset_id') if c.get('media_type')=='image' else c.get('source_video_asset_id')
            source = self.db.one('SELECT path FROM assets WHERE id=?', (aid,))
            source_path = (self.service.root/source['path']).resolve() if source else None
            missing = not source_path or not source_path.is_relative_to(self.service.root) or not source_path.is_file()
            if c.get('source_kind') == 'footage':
                result[c['edit_clip_id']] = {'missing': missing, 'stale': False, 'new_version': False}
                continue
            new_image = s and s['active_image']
            stale = bool(not s or (new_image and c.get('source_image_asset_id') != new_image) or s.get('stale') or (s.get('video_stale') and s.get('video_asset_id') == c['source_video_asset_id']))
            result[c['edit_clip_id']] = {'missing': missing, 'stale': stale, 'new_version': bool(s and s.get('video_asset_id') and s['video_asset_id'] != c.get('source_video_asset_id'))}
        return result

    def validate(self, project_id, document):
        if not isinstance(document, dict) or document.get('schema_version') != 1:
            raise DomainError('Неподдерживаемая схема монтажа.')
        doc = copy.deepcopy(document)
        if not isinstance(doc.get('clips'), list) or len(doc['clips']) > 2000:
            raise DomainError('Недопустимый список клипов.')
        known = {s['id'] for s in self.db.all('SELECT id FROM scenes WHERE project_id=?', (project_id,))}
        ids = set()
        for c in doc['clips']:
            cid = c.get('edit_clip_id')
            kind = c.get('source_kind', 'scene')
            if not isinstance(cid, str) or not cid or len(cid) > 100 or cid in ids or kind not in ('scene', 'footage') or (kind == 'scene' and c.get('scene_id') not in known):
                raise DomainError('Недопустимый клип монтажа.')
            if kind == 'footage':
                if not isinstance(c.get('title', ''), str) or len(c.get('title', '')) > 300:
                    raise DomainError('Недопустимое название футажа.')
                if c.get('origin_scene_id') is not None and c['origin_scene_id'] not in known:
                    raise DomainError('Исходная сцена не принадлежит проекту.')
                if c.get('scene_id') is not None:
                    raise DomainError('Недопустимый источник футажа.')
                if c.get('media_type') == 'image':
                    asset = self.db.one("""SELECT a.project_id FROM assets a JOIN generations g ON g.id=a.generation_id 
                        WHERE a.id=? AND g.purpose IN ('edit_image', 'edit_thumbnail', 'edit_footage', 'footage_insert')""",
                        (c.get('source_image_asset_id'),))
                    if not asset or asset['project_id'] != project_id:
                        raise DomainError('Недопустимый источник фотофутажа.')
                else:
                    asset = self.db.one("""SELECT a.project_id FROM assets a JOIN generations g ON g.id=a.generation_id 
                        WHERE a.id=? AND g.purpose IN ('edit_video', 'footage_insert')""",
                        (c.get('source_video_asset_id'),))
                    if not asset or asset['project_id'] != project_id:
                        raise DomainError('Недопустимый источник футажа.')
            ids.add(cid)
            media_type=c.get('media_type','video')
            if media_type not in ('video','image'):
                raise DomainError('Недопустимый тип источника монтажа.')
            try:
                duration = 86400000 if media_type=='image' else self.media(project_id, c.get('source_video_asset_id'), 'video')
            except DomainError:
                # An excluded, previously saved clip can outlive a removed file.
                previous = self.db.one('SELECT document_json FROM project_edits WHERE project_id=?', (project_id,))
                original = next((old for old in json.loads(previous['document_json'])['clips'] if old['edit_clip_id']==cid and old['source_video_asset_id']==c.get('source_video_asset_id')), None) if previous else None
                if not c.get('excluded') or not original:
                    raise
                duration = original['source_duration_ms']
            if media_type=='image' and (not c.get('source_image_asset_id') or c.get('source_video_asset_id')):
                raise DomainError('Изображение монтажа должно иметь отдельный источник.')
            if c.get('source_image_asset_id'):
                asset = self.service.asset(c['source_image_asset_id'])
                if asset['project_id'] != project_id or not asset['width'] or not asset['height']:
                    raise DomainError('Недопустимое изображение клипа.')
                image_path = self.service.root/asset['path']
                image_stat = image_path.stat()
                image_key = (asset['id'], image_stat.st_size, image_stat.st_mtime_ns)
                if image_key not in self._image_cache:
                    from PIL import Image
                    try:
                        with Image.open(image_path) as image:
                            if not image.format:
                                raise ValueError('not an image')
                    except Exception as error:
                        raise DomainError('Источник миниатюры должен быть изображением.') from error
                    self._image_cache.add(image_key)
            if c.get('camera_motion'):
                if media_type!='image':raise DomainError('Движение камеры требует исходного изображения.')
                from .montage_media import camera_plan
                plan=camera_plan(asset['width'],asset['height'],c['camera_motion'].get('preset'))
                if c['camera_motion']!=plan:raise DomainError('Параметры движения камеры изменены.')
            c['source_duration_ms'] = duration
            number(c.get('source_scene_revision'), 0 if kind == 'footage' else 1)
            number(c.get('trim_in_ms'), 0, duration)
            number(c.get('trim_out_ms'), 0, duration)
            if c['trim_out_ms'] - c['trim_in_ms'] < 1000 / 24:
                raise DomainError('Клип должен содержать хотя бы один кадр.')
            number(c.get('volume', 1), 0, 1)
            number(c.get('playback_rate', 1), .05, 4)
            number(c.get('hold_last_ms',0))
            if (c.get('hold_last_ms',0) or media_type=='image') and not c.get('muted',True):
                raise DomainError('При удержании кадра исходный звук клипа должен быть выключен.')
            if 'timing_locked' in c:c['timing_locked']=bool(c['timing_locked'])
            if c.get('playback_rate', 1) != 1 and not c.get('muted', True):
                raise DomainError('У клипа с изменённой скоростью отключите исходный звук. Озвучка сохраняет свою скорость.')
            c['excluded'] = bool(c.get('excluded'))
            c['muted'] = bool(c.get('muted', True))
            if c.get('framing', 'fit') not in ('fit', 'fill'):
                raise DomainError('Недопустимое кадрирование клипа.')
            if 'illustrative' in c:
                c['illustrative'] = bool(c['illustrative'])
        from .montage_layers import validate_layers
        validate_layers(self,project_id,doc)
        transitions = doc.setdefault('transitions', [])
        seen = set()
        if not isinstance(transitions, list) or len(transitions) > 2000:
            raise DomainError('Недопустимые переходы.')
        for t in transitions:
            pair = (t.get('left'), t.get('right'))
            if pair in seen or pair[0] not in ids or pair[1] not in ids or pair[0] == pair[1] or t.get('kind') not in ('cut', 'dissolve', 'black', 'white'):
                raise DomainError('Недопустимый стык.')
            seen.add(pair)
            number(t.get('duration_ms'), 0, 5000)
        tracks = doc.setdefault('audio_tracks', [])
        if not isinstance(tracks, list) or len(tracks) > 2 or len({a.get('role') for a in tracks}) != len(tracks):
            raise DomainError('Допустимы одна озвучка и одна музыка.')
        for a in tracks:
            if a.get('role') not in ('voice', 'music'):
                raise DomainError('Недопустимая звуковая дорожка.')
            duration = self.media(project_id, a.get('asset_id'), 'audio')
            a['source_duration_ms'] = duration
            number(a.get('trim_in_ms'), 0, duration)
            number(a.get('trim_out_ms'), 0, duration)
            if a['trim_out_ms'] <= a['trim_in_ms']:
                raise DomainError('Пустая звуковая дорожка.')
            for field in ('offset_ms', 'fade_in_ms', 'fade_out_ms'):
                number(a.get(field, 0))
            number(a.get('volume', 1), 0, 1)
            a['enabled'] = bool(a.get('enabled', True))
        doc['source_audio'] = bool(doc.get('source_audio'))
        if doc.get('timing_mode') not in (None,'voice_fit'):
            raise DomainError('Неизвестный режим времени монтажа.')
        if doc.get('voice_fit'):
            from .montage_voice_fit import audio_binding,source_signature
            doc['voice_fit']['stale']=(audio_binding(self.service,project_id,doc)!=doc['voice_fit'].get('audio_binding')
                or hashlib.sha256(self.service.project(project_id)['story'].encode()).hexdigest()!=doc['voice_fit'].get('story_sha256')
                or source_signature(doc)!=doc['voice_fit'].get('source_hash'))
        settings = doc.get('export_settings', {'width':1920,'height':1080,'fps':24})
        if not isinstance(settings, dict) or settings.get('fps') != 24 or (settings.get('width'),settings.get('height')) not in ((1280,720),(1920,1080)):
            raise DomainError('Выберите HD 720p или Full HD 1080p, 24 кадра/с.')
        doc['export_settings'] = {key:settings[key] for key in ('width','height','fps')}
        return doc

    def save(self, project_id, data, *, guard=None):
        self.project(project_id)
        revision = data.get('expected_revision')
        number(revision)
        doc = self.validate(project_id, data.get('document'))
        with self.db.transaction() as conn:
            row = conn.execute('SELECT revision FROM project_edits WHERE project_id=?', (project_id,)).fetchone()
            current = row['revision'] if row else 0
            if revision != current:
                raise DomainError('Монтаж изменён в другой вкладке. Черновик сохранён локально; перечитайте монтаж.', 409)
            if guard:guard()
            conn.execute('INSERT INTO project_edits VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision, document_json=excluded.document_json, updated_at=excluded.updated_at', (project_id, current+1, 1, dump(doc), now()))
        return {'revision': current+1, 'document':doc, 'timeline': normalize(doc), 'sources':self.sources(project_id,doc)}

    def sync(self, project_id, data):
        current = self.get(project_id)
        if data.get('expected_revision') != current['revision']:
            raise DomainError('Монтаж изменён в другой вкладке.', 409)
        doc = copy.deepcopy(data.get('document', current['document']))
        doc = self.validate(project_id, doc)
        scenes = self.service.detail(project_id)['scenes']
        by_scene = {c['scene_id']: c for c in doc['clips'] if c.get('source_kind', 'scene') == 'scene'}
        independent_scenes = {c.get('origin_scene_id') for c in doc['clips'] if c.get('source_kind') == 'footage'}
        selected = data.get('clip_ids')
        changes = []
        for s in scenes:
            video = s.get('video_asset_id')
            image=(self.db.one('SELECT asset_id FROM generations WHERE id=?',(s.get('active_generation_id'),)) or {}).get('asset_id')
            if s.get('stale') or (video and s.get('video_stale')) or (not video and not image):
                continue
            old = by_scene.get(s['id'])
            if not old and s['id'] in independent_scenes:
                continue
            if old and (data.get('add_only') or (selected is not None and old['edit_clip_id'] not in selected)):
                continue
            if not old and selected is not None:
                continue
            duration = self.media(project_id, video, 'video') if video else 86400000
            generation = self.db.one('SELECT asset_id FROM generations WHERE id=?', (s.get('active_generation_id'),))
            planned_seconds = (s.get('data', {}).get('prompt_timing') or {}).get('duration_seconds', 5)
            planned_ms = round(planned_seconds*1000) if type(planned_seconds) in (int, float) and 1 <= planned_seconds <= 60 else 5000
            initial_ms = min(duration, planned_ms) if video and s.get('data', {}).get('prompt_timing') else duration if video else planned_ms
            c = old or {'edit_clip_id':uid(),'scene_id':s['id'],'trim_in_ms':0,'trim_out_ms':initial_ms,'excluded':False,'volume':1,'muted':True}
            c.update(source_video_asset_id=video, source_image_asset_id=(generation or {}).get('asset_id'), source_scene_revision=s['current_revision'], source_duration_ms=duration)
            if video:c.pop('media_type',None);c.pop('camera_motion',None)
            else:c['media_type']='image'
            if c['trim_out_ms'] > duration or c['trim_in_ms'] >= duration-1000/24:
                c['trim_out_ms'] = duration
                c['trim_in_ms'] = min(c['trim_in_ms'], max(0, duration-1000/24))
                changes.append(c['edit_clip_id'])
            if not old:
                doc['clips'].append(c)
        # Return a reviewable document; saving is a separate undoable UI operation.
        return {'document':self.validate(project_id,doc), 'adjusted_clips':changes}

    def import_video_file(self, project_id, source, filename):
        """Copy local uploads into project media; never alter generation scenes."""
        self.project(project_id)
        source = Path(source)
        title = Path(filename.replace('\\', '/')).name[:300]
        if source.stat().st_size > 512_000_000 or Path(title).suffix.lower() not in ('.mp4', '.mov', '.m4v', '.mkv', '.webm'):
            raise DomainError('Выберите MP4, MOV, M4V, MKV или WebM не больше 512 МБ.', 413)
        duration = probe(source, 'video')
        try:
            with av.open(str(source)) as media:
                stream = media.streams.video[0]
                first = next(media.decode(stream))
                compatible = stream.codec_context.name == 'h264' and first.format.name == 'yuv420p' and media.format.name in ('mov,mp4,m4a,3gp,3g2,mj2', 'mp4')
                has_audio = bool(media.streams.audio)
                media.seek(max(0, int(((stream.start_time or 0)*stream.time_base + duration/1000 - .5)/stream.time_base)), stream=stream, backward=True)
                next(media.decode(stream))
        except Exception as error:
            raise DomainError('Видеофайл повреждён или не содержит кадров.') from error
        aid, gid, thumb, thumb_gid = uid(), uid(), uid(), uid()
        path = Path('assets')/project_id/(aid+'.mp4')
        thumb_path = path.with_name(thumb+'.jpg')
        target, thumbnail = self.service.root/path, self.service.root/thumb_path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if compatible:
                shutil.copyfile(source, target)
            else:
                from types import SimpleNamespace
                from .export_worker import render
                adapter = SimpleNamespace(root=source.parent, asset=lambda _: {'path': source.name})
                doc = {'clips':[{'edit_clip_id':aid, 'source_video_asset_id':aid, 'trim_in_ms':0, 'trim_out_ms':duration, 'muted':not has_audio, 'volume':1}], 'transitions':[], 'audio_tracks':[], 'source_audio':has_audio}
                render(adapter, doc, target, lambda *_:None, lambda:False, size=(1920,1080))
            duration = probe(target, 'video')
            with av.open(str(target)) as media:
                image = next(media.decode(video=0)).to_image()
                width, height = image.size
                image.thumbnail((320,180))
                image.convert('RGB').save(thumbnail, quality=85)
            with self.db.transaction() as conn:
                for asset_id, generation_id, relative, w, h, purpose in ((aid,gid,path,width,height,'edit_video'), (thumb,thumb_gid,thumb_path,image.width,image.height,'edit_thumbnail')):
                    file = self.service.root/relative
                    with file.open('rb') as raw:
                        sha = hashlib.file_digest(raw, 'sha256').hexdigest()
                    conn.execute('INSERT INTO generations(id,project_id,purpose,state,snapshot,seed,asset_id,created_at) VALUES(?,?,?,\'succeeded\',\'{}\',0,?,?)', (generation_id,project_id,purpose,asset_id,now()))
                    conn.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)', (asset_id,project_id,generation_id,str(relative),sha,w,h,file.stat().st_size))
        except Exception:
            target.unlink(missing_ok=True)
            thumbnail.unlink(missing_ok=True)
            raise
        clip = {'edit_clip_id':uid(), 'source_kind':'footage', 'scene_id':None, 'title':title, 'source_video_asset_id':aid, 'source_image_asset_id':thumb, 'source_scene_revision':0, 'source_duration_ms':duration, 'trim_in_ms':0, 'trim_out_ms':duration, 'excluded':False, 'muted':True, 'volume':1}
        return {'clip':clip, 'asset_id':aid, 'duration_ms':duration}

    def audio(self, project_id, data):
        self.project(project_id)
        import base64
        payload = data.get('data', '')
        try:
            raw = base64.b64decode(payload.split(',')[-1], validate=True)
        except Exception as error:
            raise DomainError('Некорректный аудиофайл.') from error
        if not 0 < len(raw) <= 100_000_000:
            raise DomainError('Аудиофайл должен быть не больше 100 МБ.', 413)
        duration = probe(io.BytesIO(raw), 'audio')
        # Decode every packet before accepting an upload; bounded by file size.
        try:
            with av.open(io.BytesIO(raw)) as media:
                count = sum(1 for _ in media.decode(audio=0))
                if not count:
                    raise ValueError('empty')
        except Exception as error:
            raise DomainError('Аудиофайл повреждён.') from error
        suffix = Path(data.get('filename', 'audio.wav')).suffix.lower()
        if suffix not in ('.wav','.mp3','.m4a','.aac','.mp4','.ogg','.flac'):
            raise DomainError('Поддерживаются WAV, MP3, M4A, AAC, MP4, OGG и FLAC.')
        aid, gid = uid(), uid()
        path = Path('assets') / project_id / (aid + suffix)
        target = self.service.root / path
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(raw)
        try:
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO generations(id,project_id,purpose,state,snapshot,seed,asset_id,created_at) VALUES(?,?,'edit_audio','succeeded','{}',0,?,?)", (gid,project_id,aid,now()))
                conn.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',(aid,project_id,gid,str(path),hashlib.sha256(raw).hexdigest(),0,0,len(raw)))
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {'asset_id':aid,'duration_ms':duration}
