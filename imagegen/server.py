"""Loopback-only HTTP adapter. No internet-facing deployment is supported."""
import json
import logging
import mimetypes
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from .db import dump
from .domain import DomainError, MAX_SCENES
from .providers import ComfyProvider, GoogleNanoBananaProvider, ProviderError
from .runtime import RuntimeManager
from .service import Service
from .worker import Worker

log = logging.getLogger(__name__)


def _resolve_static_dir():
    import sys
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            p = Path(sys._MEIPASS) / 'imagegen' / 'static'
            if p.is_dir():
                return p
            p = Path(sys._MEIPASS) / 'static'
            if p.is_dir():
                return p
        candidates = [
            Path(sys.executable).resolve().parent / 'static',
            Path(sys.executable).resolve().parent / '_internal' / 'imagegen' / 'static',
            Path(sys.executable).resolve().parent / '_internal' / 'static',
        ]
        for c in candidates:
            if c.is_dir():
                return c
    return Path(__file__).parent / 'static'


STATIC = _resolve_static_dir()
# Background Flow tabs may send their timer heartbeat only once a minute.
BRIDGE_ADAPTER_TTL = 90


import base64

def extract_image_bytes(data):
    if isinstance(data, bytes):
        raw = data
    elif isinstance(data, dict):
        raw = data.get('video') or data.get('video_base64') or data.get('image') or data.get('image_base64') or data.get('data') or data.get('file') or ''
    elif isinstance(data, str):
        raw = data
    else:
        raise DomainError('Некорректный формат данных медиа.')
    if isinstance(raw, str):
        if ',' in raw:
            raw = raw.split(',', 1)[1]
        try:
            return base64.b64decode(raw)
        except Exception as error:
            raise DomainError(f'Ошибка декодирования base64: {error}')
    if isinstance(raw, bytes):
        return raw
    raise DomainError('Медиа-данные не предоставлены.')


class Application:
    def __init__(self, root):
        self.service = Service(root)
        from .export_worker import ExportWorker
        self.edit_exports = ExportWorker(self.service)
        from .montage_voice_fit import MontageVoiceFit
        self.montage_voice_fit = MontageVoiceFit(self.service)
        import sys
        app_root = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
        try:
            self.runtime = RuntimeManager(app_root, data_root=root)
        except TypeError:
            self.runtime = RuntimeManager(app_root)
        self.worker = Worker(self.service, runtime=self.runtime)
        from .google_queue import GoogleQueueWorker
        self.google_queue_worker = GoogleQueueWorker(self.service)
        self.token = secrets.token_urlsafe(32)
        self.mutation_lock = threading.Lock()
        self.flow_bridge_v1_lock = threading.Lock()
        self.flow_bridge_v1 = {'last_seen': 0, 'project_id': None, 'flow_project_id': None, 'status': '', 'running': False, 'adapter_connected': False, 'adapter_instance_id': None, 'script_version': None, 'scoped_scene_start': False, 'command': None, 'last_ack_command_id': None, 'last_ack_at': 0}
        from .voice_workflow import VoiceWorkflow
        self.voice = VoiceWorkflow(self.service, self.edit_exports,
            lambda action, pid, ids, reference_ids=None, images_only=False: self.dispatch('POST', '/api/bridge/v1/command',
                {'action':action, 'project_id':pid, 'images_only':images_only, **({'scene_ids':ids} if ids is not None else {}),
                 **({'generation_ids':reference_ids} if reference_ids is not None else {})}))
        self.flow_bridge = {
            'last_seen': 0.0,
            'status': 'Ожидание подключения Google Flow...',
            'running': False,
            'project_id': None,
            'command': None,
        }


    def dispatch(self, method, path, data, query_params=None, client=None):
        if path == '/api/voice/capability' and method == 'GET':
            return self.voice.capability()
        service = self.service
        parts = path.strip('/').split('/')
        if path.startswith('/api/maintenance/'):
            from .project_maintenance import ProjectMaintenance
            maintenance=ProjectMaintenance(self)
            if method=='GET' and path=='/api/maintenance/projects':return maintenance.list_projects()
            if method in ('POST','DELETE') and path in ('/api/maintenance/projects/delete-all','/api/maintenance/projects/delete_all'):
                return maintenance.delete_all_projects(data or {'confirm':True})
            if method=='GET' and path=='/api/maintenance/cache':return maintenance.cache_info()
            if method=='POST' and path=='/api/maintenance/cache/clear':return maintenance.clear_cache(data or {})
            if method in ('POST','DELETE') and len(parts)==5 and parts[2]=='projects':
                if parts[4] in ('trash','delete'):return maintenance.delete_project(parts[3],data or {'confirm':True})
                if parts[4]=='restore':return maintenance.restore(parts[3])
            raise DomainError('Не найдено.',404)
        if len(parts)>=3 and parts[0]=='api' and parts[1] in ('scenes','characters','locations','props','generations'):
            owner=service.db.one('SELECT project_id FROM '+parts[1]+' WHERE id=?',(parts[2],))
            if owner:service.project(owner['project_id'])
        if method == 'GET' and path == '/api/editorial-capabilities':
            return {'version': 2, 'scoped_scene_batches': True, 'application': 'FrameForge', 'release_version': '0.2.0rc3', 'bridge_version': '0.1.89'}
        if method == 'GET' and path == '/api/session':
            return {'token': self.token}
        if method == 'GET' and path == '/api/settings':
            from .service import public_settings
            return public_settings(service.settings(), service.root)
        if method == 'POST' and path == '/api/settings':
            from .service import public_settings
            return public_settings(service.save_settings(data), service.root)
        if parts[:3] == ['api', 'stock', 'pixabay']:
            from .stock_secrets import set_pixabay_key, delete_pixabay_key, check_pixabay_connection, get_pixabay_key, is_pixabay_key_configured
            if len(parts) == 4 and parts[3] == 'key' and method == 'POST':
                key = (data or {}).get('key', '')
                set_pixabay_key(service.root, key)
                return {'ok': True, 'pixabay_key_configured': is_pixabay_key_configured(service.root)}
            if len(parts) == 5 and parts[3:5] == ['key', 'delete'] and method == 'POST':
                delete_pixabay_key(service.root)
                return {'ok': True, 'pixabay_key_configured': False}
            if len(parts) == 4 and parts[3] == 'check' and method == 'POST':
                key = (data or {}).get('key')
                if not key:
                    key = get_pixabay_key(service.root)
                if not key:
                    return {'ok': False, 'status': 'not_configured', 'error': 'Ключ Pixabay API не настроен.'}
                return check_pixabay_connection(key)
            raise DomainError('Ресурс не найден.', 404)
        if parts == ['api', 'material-search', 'universes'] and method == 'GET':
            from .material_search.query_builder import UNIVERSES
            return {'ok': True, 'universes': [{'id': k, **v} for k, v in UNIVERSES.items()]}
        if parts == ['api', 'material-search', 'providers'] and method == 'GET':
            return {'ok': True, 'providers': service.material_search.list_providers()}
        if method == 'GET' and path == '/api/google-queue':
            from .google_queue import configured_queue
            queue = configured_queue(service.root,service.settings())
            return {'enabled':False,'jobs':[],'usage':[]} if queue is None else {'enabled':True,**queue.overview()}
        if method=='GET' and len(parts)==4 and parts[:2]==['api','projects'] and parts[3]=='google-review-summary':
            return service.google_review_summary(parts[2])
        if len(parts)==5 and parts[:2]==['api','projects'] and parts[3:]==['google-queue','recovery']:
            service.project(parts[2])
            from .google_queue import configured_queue
            queue=configured_queue(service.root,service.settings())
            if queue is None:raise DomainError('Новая очередь Google выключена.',409)
            if method=='GET':return {'project_id':parts[2],'paused':queue.project_paused(parts[2]),'jobs':queue.overview(parts[2])['recovery_jobs']}
            if method=='POST':
                data=data or {}
                try:
                    result=queue.recover(data.get('job_id'),parts[2],data.get('expected_state'),data.get('action'),data.get('note'))
                except KeyError:raise DomainError('Проверка этого проекта не найдена.',404) from None
                except ValueError as error:raise DomainError(str(error),409) from None
                return {**result,'media_generation_started':False,'saved_media_changed':False}
        if len(parts)==4 and parts[:2]==['api','projects'] and parts[3]=='source-catalog':
            from .source_catalog import SourceCatalog
            catalog=SourceCatalog(service)
            if method=='GET':return catalog.view(parts[2])
            if method=='POST':return catalog.sync(parts[2],(data or {}).get('project_version'))
        if method=='POST' and len(parts)==5 and parts[:2]==['api','projects'] and parts[3:]==['source-catalog','bind']:
            from .source_catalog import SourceCatalog
            data=data or {}
            return SourceCatalog(service).bind(parts[2],data.get('anchor_id'),data.get('entry_id'),data.get('revision'),data.get('expected_binding'))
        if parts[:2]==['api','source-catalog'] and len(parts) in (3,4):
            from .source_catalog import SourceCatalog
            catalog=SourceCatalog(service)
            if method=='GET' and len(parts)==3:return catalog.get(parts[2])
            if method=='POST' and len(parts)==4 and parts[3]=='revise':
                return catalog.revise(parts[2],(data or {}).get('expected_revision'),(data or {}).get('changes'))
            if method=='POST' and len(parts)==4 and parts[3]=='check':
                data=data or {}
                return catalog.record_check(parts[2],data.get('expected_revision'),data.get('claim'),data.get('verdict'),data.get('sources'))
        if method=='GET' and len(parts)==4 and parts[:2]==['api','projects'] and parts[3]=='source-passport':
            from .source_contract import passport
            project=service.detail(parts[2]);return passport(project,project['scenes'])
        if method == 'POST' and len(parts)==5 and parts[:2]==['api','projects'] and parts[3:] == ['google-queue','pause']:
            service.project(parts[2])
            from .google_queue import configured_queue
            queue = configured_queue(service.root,service.settings())
            if queue is None:
                raise DomainError('Новая очередь Google выключена.',409)
            paused = (data or {}).get('paused')
            if type(paused) is not bool:
                raise DomainError('Укажите paused=true или paused=false.')
            queue.pause(parts[2],paused)
            return {'project_id':parts[2],'paused':paused}
        if method == 'GET' and path == '/api/health':
            settings = service.settings()
            result = {}
            for name, provider in [('comfy', ComfyProvider(settings['comfy_url']))]:
                try:
                    result[name] = {'ok': True, 'detail': provider.health()}
                except ProviderError as error:
                    result[name] = {'ok': False, 'error': str(error)}
            deepseek_key = settings.get('deepseek_api_key') or os.environ.get('DEEPSEEK_API_KEY')
            result['text'] = {
                'ok': bool(deepseek_key),
                'backend': 'deepseek',
                'model': 'deepseek-flash',
                'detail': 'DeepSeek API key is configured' if deepseek_key else 'DeepSeek API key is missing'
            }
            if settings.get('google_api_key'):
                try:
                    from .google_queue import configured_queue
                    provider = GoogleNanoBananaProvider(settings['google_api_key'], settings.get('google_model'), settings.get('google_extra_api_keys', ''),
                        dispatcher=configured_queue(service.root,settings))
                    result['google_nanobanana'] = {'ok': True, 'detail': provider.health()}
                except (ProviderError, DomainError) as error:
                    result['google_nanobanana'] = {'ok': False, 'error': str(error)}
            return result
        if method == 'GET' and path == '/api/models':
            return ComfyProvider(service.settings()['comfy_url']).models()
        if path == '/api/telemetry' and method == 'POST':
            log.info('Telemetry from client: %s', dump(data))
            return {'status': 'ok', 'received': True}


        if path == '/api/bridge/v1/status' and method == 'GET':
            with self.flow_bridge_v1_lock:
                bridge = self.flow_bridge_v1.copy()
            return {
                'connected': time.time() - bridge['last_seen'] < BRIDGE_ADAPTER_TTL and bridge['adapter_connected'],
                'project_id': bridge['project_id'],
                'flow_project_id': bridge['flow_project_id'],
                'status': bridge['status'],
                'running': bridge['running'],
                'adapter_instance_id': bridge['adapter_instance_id'],
                'script_version': bridge['script_version'],
                'scoped_scene_start': bridge['scoped_scene_start'],
                'last_ack_command_id': bridge.get('last_ack_command_id'),
                'pending_command': bridge['command']['action'] if bridge['command'] else None,
                'reference_casting': bridge.get('reference_casting', False),
                'character_casting': bridge.get('character_casting', False),
                'staged_image_batch': bridge.get('staged_image_batch', False),
                'image_concurrency': bridge.get('image_concurrency', 1),
                'active_image_requests': bridge.get('active_image_requests', 0),
                'animations_only': bridge.get('animations_only', False),
                'video_recovery': bridge.get('video_recovery', False),
                'animation_direction_refresh': bridge.get('animation_direction_refresh', False),
                'animation_direction_contract': bridge.get('animation_direction_contract'),
            }
        if path == '/api/bridge/v1/heartbeat' and method == 'POST':
            project_id = (data or {}).get('project_id')
            flow_project_id = (data or {}).get('flow_project_id')
            adapter_instance_id = (data or {}).get('adapter_instance_id')
            if not isinstance(project_id, str) or not isinstance(flow_project_id, str) or len(project_id) > 100 or len(flow_project_id) > 100:
                raise DomainError('Некорректный проект моста.', 400)
            if adapter_instance_id is not None and (not isinstance(adapter_instance_id, str) or len(adapter_instance_id) > 100):
                raise DomainError('Некорректный экземпляр моста.', 400)
            with self.flow_bridge_v1_lock:
                bridge = self.flow_bridge_v1
                adapter_connected = bool(data.get('adapter_connected'))
                # A Flow project can be open in several tabs.  The user selects
                # the live adapter through “Проверить связь”; only that tab can
                # consume commands.  A legacy script has no instance id and is
                # deliberately unable to replace an explicitly selected adapter.
                if data.get('claim_adapter') and adapter_connected and adapter_instance_id:
                    bridge['adapter_instance_id'] = adapter_instance_id
                elif (bridge['adapter_instance_id'] is None or time.time() - bridge['last_seen'] >= BRIDGE_ADAPTER_TTL) and adapter_connected:
                    bridge['adapter_instance_id'] = adapter_instance_id or f'legacy:{flow_project_id}'
                current_id = adapter_instance_id or f'legacy:{flow_project_id}'
                owns_adapter = adapter_connected and bridge['adapter_instance_id'] == current_id
                if owns_adapter or bridge['adapter_instance_id'] is None or time.time() - bridge['last_seen'] >= BRIDGE_ADAPTER_TTL:
                    if owns_adapter and bridge.get('animation_scope_started') and not data.get('running'):
                        bridge['animation_scene_ids'] = None
                        bridge['animation_scope_started'] = False
                    elif owns_adapter and data.get('running') and bridge.get('animation_scene_ids') is not None:
                        bridge['animation_scope_started'] = True
                    bridge.update(last_seen=time.time(), project_id=project_id, flow_project_id=flow_project_id,
                                  status=str(data.get('status', ''))[:300], running=bool(data.get('running')),
                                  adapter_connected=adapter_connected,
                                  script_version=str(data.get('script_version') or '')[:30] or None,
                                  scoped_scene_start=data.get('scoped_scene_start') is True,
                                  reference_casting=data.get('reference_casting') is True)
                    bridge['character_casting']=data.get('character_casting') is True
                    bridge['quota_admission']=data.get('quota_admission') is True
                    bridge['images_only']=data.get('images_only') is True
                    bridge['animations_only']=data.get('animations_only') is True
                    bridge['video_recovery']=data.get('video_recovery') is True
                    bridge['animation_direction_refresh']=data.get('animation_direction_refresh') is True
                    bridge['animation_direction_contract']=data.get('animation_direction_contract') if data.get('animation_direction_contract') == 'image-motion/3' else None
                    bridge['staged_image_batch']=data.get('staged_image_batch') is True
                    concurrency=data.get('image_concurrency',1)
                    bridge['image_concurrency']=min(4,max(1,concurrency)) if type(concurrency) is int else 1
                    active=data.get('active_image_requests',0)
                    bridge['active_image_requests']=min(4,max(0,active)) if type(active) is int else 0
                command = bridge['command'] if owns_adapter else None
                if command and data.get('ack_command_id') == command['id']:
                    bridge['last_ack_command_id'] = command['id']
                    bridge['last_ack_at'] = time.time()
                    bridge['command'] = None
                    command = None
                return {'command': command}
        if path == '/api/bridge/v1/command' and method == 'POST':
            action = (data or {}).get('action')
            project_id = (data or {}).get('project_id')
            scene_ids = (data or {}).get('scene_ids')
            generation_ids = (data or {}).get('generation_ids')
            batch_stage = (data or {}).get('batch_stage')
            animations_only = data.get('animations_only') is True
            if animations_only and (action != 'start' or not scene_ids or generation_ids is not None
                                    or batch_stage is not None or data.get('images_only')):
                raise DomainError('Для запуска анимации выберите кадры без режима генерации изображений.', 400)
            if action not in ('start', 'pause', 'check') or not isinstance(project_id, str):
                raise DomainError('Некорректная команда моста.', 400)
            forgotten = service._project_profile_type(project_id) == 'forgotten_cities'
            if batch_stage is not None:
                if (batch_stage not in ('portraits', 'scenes') or action != 'start'
                        or scene_ids is not None or generation_ids is not None or data.get('images_only') is not True):
                    raise DomainError('Некорректный этап генерации Flow.', 400)
                if batch_stage == 'portraits' and forgotten:
                    raise DomainError('В этом профиле эталоны создаются в разделе локаций и реквизита.', 409)
                if batch_stage == 'scenes':
                    detail = service.detail(project_id)
                    if any(req['kind'] == 'character' and not req['ready'] for req in detail['reference_requirements']):
                        raise DomainError('Проверьте и закрепите обязательные портреты перед запуском сцен.', 409)
                    if not detail['preflight'].get('ok'):
                        errors = detail['preflight'].get('errors') or []
                        raise DomainError('Генерация сцен заблокирована: ' + '; '.join(
                            error.get('message') or error.get('code', '') for error in errors[:3]
                        ), 409)
            character_cast = False
            if generation_ids is not None:
                if (action != 'start' or scene_ids is not None or not isinstance(generation_ids, list)
                        or len(generation_ids) not in (1,2) or not all(isinstance(gid, str) for gid in generation_ids)
                        or len(set(generation_ids)) != len(generation_ids)):
                    raise DomainError('Выберите одну пару вариантов эталона.')
                character_cast = len(generation_ids)==1 and bool(service.db.one("SELECT id FROM generations WHERE id=? AND project_id=? AND purpose='casting' AND state IN ('queued','running') AND cancel_requested=0",(generation_ids[0],project_id)))
                session = character_cast or service.db.one('SELECT * FROM reference_casts WHERE project_id=? AND generation_ids=?', (project_id, dump(generation_ids)))
                if not session:
                    raise DomainError('Кастинг не принадлежит проекту.', 409)
            elif forgotten and action == 'start' and scene_ids is None and batch_stage is None:
                if not service.project_preflight(project_id).get('ok'):
                    raise DomainError('Сначала закрепите обязательные эталоны.', 409)
            if scene_ids is not None:
                if (action != 'start' or not isinstance(scene_ids, list) or not 1 <= len(scene_ids) <= MAX_SCENES
                        or not all(isinstance(sid, str) for sid in scene_ids) or len(set(scene_ids)) != len(scene_ids)):
                    raise DomainError('Выберите неповторяющиеся кадры для точечной генерации.', 400)
                known = {row['id'] for row in service.db.all('SELECT id FROM scenes WHERE project_id=?', (project_id,))}
                if not set(scene_ids) <= known:
                    raise DomainError('Кадр не принадлежит открытому проекту.', 404)
            active = getattr(service, 'active_project_id', None)
            if project_id != active:
                raise DomainError('Откройте выбранный проект ФФ перед проверкой моста.', 409)
            with self.flow_bridge_v1_lock:
                bridge = self.flow_bridge_v1
                unavailable = (time.time() - bridge['last_seen'] >= BRIDGE_ADAPTER_TTL or not bridge['adapter_connected'] or not bridge['adapter_instance_id'])
                if unavailable:
                    raise DomainError('Мост Flow не подключён. Откройте FrameForge Bridge и дождитесь подключения.', 409)
                if batch_stage is not None:
                    if not bridge.get('staged_image_batch'):
                        raise DomainError('Обновите FrameForge Flow Bridge до 0.1.82 и перезагрузите вкладку Flow.', 409)
                    if bridge['running'] or bridge['command'] or bridge.get('active_image_requests'):
                        raise DomainError('Flow занят. Дождитесь завершения текущей операции.', 409)
                if character_cast and not bridge.get('character_casting'):
                    raise DomainError('Обновите FrameForge Flow Bridge до 0.1.76 и перезагрузите вкладку Flow для одного нового портрета.',409)
                if action == 'start' and bridge.get('running') and character_cast:
                    raise DomainError('Мост завершает текущий запрос. Новый портрет пока не отправлен.',409)
                if action == 'start' and (forgotten or generation_ids is not None) and not bridge.get('reference_casting'):
                    raise DomainError('Обновите userscript Flow Bridge: требуется изолированный кастинг эталонов.', 409)
                if action=='start' and data.get('images_only') is True and not bridge.get('images_only'):
                    raise DomainError('Для подготовки только кадров нужен обновлённый мост Flow.',409)
                if animations_only and not bridge.get('animations_only'):
                    raise DomainError('Обновите userscript FrameForge Flow Bridge до 0.1.84 и перезагрузите вкладку Flow: требуется отдельная очередь анимации.', 409)
                if (action == 'start' and generation_ids is None and batch_stage is None
                        and not data.get('images_only') and bridge.get('animation_direction_contract') != 'image-motion/3'):
                    pending = service.db.all('''SELECT s.id,r.data FROM scenes s JOIN scene_revisions r
                        ON r.scene_id=s.id AND r.revision=s.current_revision WHERE s.project_id=?
                        AND s.video_asset_id IS NULL''', (project_id,))
                    if any((json.loads(row['data']).get('animation') or {}).get('status') in ('queued_omni','queued_veo')
                           and (json.loads(row['data']).get('animation') or {}).get('prompt_contract') != 'image-motion/3'
                           for row in pending if scene_ids is None or row['id'] in scene_ids):
                        raise DomainError('Обновите FrameForge Flow Bridge до 0.1.86 и перезагрузите Flow: требуется безопасное обновление старых видео-промптов.', 409)
                if animations_only and (bridge['running'] or bridge['command'] or bridge.get('active_image_requests')):
                    raise DomainError('Flow занят. Дождитесь завершения текущего запроса.', 409)
                if action=='start' and not animations_only and generation_ids is None and batch_stage != 'portraits' and service.settings().get('google_queue_enabled') is True and not bridge.get('quota_admission'):
                    raise DomainError('Для новой очереди нужен обновлённый мост Flow с ожиданием проверки кадров.',409)
                if action != 'check' and bridge['project_id'] != project_id:
                    raise DomainError('Мост Flow не подключён к выбранному проекту. Нажмите «Проверить мост».', 409)
                if scene_ids is not None and not bridge['scoped_scene_start']:
                    raise DomainError('Обновите userscript FrameForge Flow Bridge и перезагрузите вкладку Flow: требуется запуск отдельного кадра.', 409)
                command = {'id': secrets.token_urlsafe(12), 'action': action, 'project_id': project_id,
                           'scene_ids': scene_ids, 'generation_ids': generation_ids, 'images_only':data.get('images_only') is True}
                if batch_stage is not None:
                    command['batch_stage'] = batch_stage
                if animations_only:
                    command['animations_only'] = True
                bridge['command'] = command
                if action == 'start':
                    bridge['animation_scene_ids'] = scene_ids
                    bridge['animation_scope_started'] = False
                if action == 'check':
                    bridge['status'] = 'Проверка и привязка к выбранному проекту ФФ...'
                return {'ok': True, 'command_id': command['id']}
        if path == '/api/bridge/v1/admission/failure' and method == 'POST':
            from .flow_admission import FlowAdmission
            return FlowAdmission(service).fail(data.get('scene_id'),data.get('request_id'),data.get('code'))
        if path == '/api/bridge/v1/admission' and method == 'POST':
            from .flow_admission import FlowAdmission
            return FlowAdmission(service).claim(data.get('scene_id'),data.get('request_id'),data.get('revision'),data.get('prompt'))
        if path == '/api/flow/status':
            if method == 'GET':
                is_connected = (time.time() - self.flow_bridge['last_seen']) < 6.0
                return {
                    'connected': is_connected,
                    'status': self.flow_bridge['status'] if is_connected else 'Google Flow не подключен (откройте flow.google.com)',
                    'running': self.flow_bridge['running'] if is_connected else False,
                    'project_id': self.flow_bridge['project_id'],
                    'last_seen': self.flow_bridge['last_seen'],
                }
            if method == 'POST':
                self.flow_bridge['last_seen'] = time.time()
                if data:
                    if 'status' in data:
                        self.flow_bridge['status'] = str(data['status'])
                    if 'running' in data:
                        self.flow_bridge['running'] = bool(data['running'])
                    if 'project_id' in data:
                        self.flow_bridge['project_id'] = data['project_id']
                cmd = self.flow_bridge.get('command')
                payload = self.flow_bridge.get('command_payload', {})
                self.flow_bridge['command'] = None
                self.flow_bridge['command_payload'] = None
                return {'ok': True, 'command': cmd, 'payload': payload}
        if path == '/api/flow/command' and method == 'POST':
            cmd = (data or {}).get('command')
            scene_id = (data or {}).get('scene_id')
            strategy = (data or {}).get('strategy')
            allowed = ('start_full', 'start_interactive', 'start_scenes', 'stop', 'animate_scene', 'animate_batch')
            if cmd not in allowed:
                raise DomainError(f'Неизвестная команда: {cmd}', 400)
            self.flow_bridge['command'] = cmd
            self.flow_bridge['command_payload'] = {'scene_id': scene_id, 'strategy': strategy}
            if cmd == 'stop':
                self.flow_bridge['running'] = False
                self.flow_bridge['status'] = 'Остановка автопилота...'
            elif cmd == 'start_full':
                self.flow_bridge['running'] = True
                self.flow_bridge['status'] = 'Запуск полного автопилота...'
            elif cmd == 'start_interactive':
                self.flow_bridge['running'] = True
                self.flow_bridge['status'] = 'Запуск автопилота с выбором...'
            elif cmd == 'start_scenes':
                self.flow_bridge['running'] = True
                self.flow_bridge['status'] = 'Запуск генерации кадров...'
            elif cmd in ('animate_scene', 'animate_batch'):
                self.flow_bridge['running'] = True
                self.flow_bridge['status'] = f'Запуск анимации ({cmd})...'
            return {'ok': True, 'queued_command': cmd}
        if path == '/api/projects/current' and method == 'POST':
            project_id = (data or {}).get('project_id')
            if not isinstance(project_id, str) or not project_id.strip():
                raise DomainError('Выберите существующий проект.', 400)
            service.project(project_id)
            service.active_project_id = project_id
            return {'project_id': project_id}
        if path == '/api/projects/current' and method == 'GET':
            active = getattr(service, 'active_project_id', None)
            if active and service.db.one('SELECT project_id FROM project_trash WHERE project_id=?',(active,)):active=None
            if not active:
                latest = service.db.all('SELECT id FROM projects WHERE NOT EXISTS (SELECT 1 FROM project_trash WHERE project_id=projects.id) ORDER BY created_at DESC LIMIT 1')
                active = latest[0]['id'] if latest else None
                # The bridge may reconnect before a browser reload restores the
                # selected project.  Persist the fallback so its check command
                # is scoped to that same project instead of failing as inactive.
                service.active_project_id = active
            return {'project_id': active}
        if path == '/api/projects/from-prompts' and method == 'POST':
            from .prompt_import import create as create_from_prompts
            result = create_from_prompts(service, data or {})
            service.active_project_id = result['id']
            return result
        if path == '/api/projects':
            if method == 'GET':
                return service.db.all('SELECT id,title,created_at FROM projects WHERE NOT EXISTS (SELECT 1 FROM project_trash WHERE project_id=projects.id) ORDER BY created_at DESC')
            if method == 'POST':
                res = service.create_project(data)
                if isinstance(res, dict) and 'id' in res:
                    service.active_project_id = res['id']
                return res
        if path == '/api/channels':
            if method == 'GET':
                include_archived = False
                if query_params and any(v in ('1', 'true', 'True') for v in query_params.get('include_archived', [])):
                    include_archived = True
                return service.list_channels(include_archived=include_archived)
            if method == 'POST':
                return service.create_channel(data or {})
        if len(parts) >= 3 and parts[:2] == ['api', 'channels']:
            channel_id = parts[2]
            if len(parts) == 3:
                if method == 'GET':
                    return service.get_channel(channel_id)
                if method in ('PUT', 'POST'):
                    return service.update_channel(channel_id, data or {})
            if len(parts) == 4 and method == 'POST':
                action = parts[3]
                if action == 'update':
                    return service.update_channel(channel_id, data or {})
                if action == 'duplicate':
                    return service.duplicate_channel(channel_id, (data or {}).get('name'))
                if action == 'archive':
                    return service.archive_channel(channel_id)
                if action == 'unarchive':
                    return service.unarchive_channel(channel_id)
                if action == 'versions':
                    return service.save_channel_version(channel_id, data or {})
                if action == 'characters':
                    return service.save_channel_character(channel_id, data or {})
                if action == 'references':
                    return service.upload_channel_reference(
                        channel_id,
                        (data or {}).get('character_id'),
                        (data or {}).get('asset_type', 'character_front'),
                        extract_image_bytes(data),
                        filename=(data or {}).get('filename'),
                        is_primary=bool((data or {}).get('is_primary'))
                    )
            if len(parts) >= 5 and parts[3] == 'references':
                asset_id = parts[4]
                if method in ('DELETE', 'POST'):
                    return service.delete_channel_reference(channel_id, asset_id)
        if len(parts) >= 3 and parts[:2] == ['api', 'projects']:
            project_id = parts[2]
            service.project(project_id)
            if len(parts)==5 and parts[3:]==['voice','narration-text'] and method=='GET':
                return self.voice.narration_text(project_id)
            if len(parts) == 4 and parts[3] == 'voice':
                if method == 'GET': return self.voice.get(project_id)
                if method == 'POST': return self.voice.start(project_id, data or {})
            if len(parts)>=5 and parts[3:5]==['voice','plans']:
                from .voice_plans import VoicePlans
                plans=VoicePlans(self.voice)
                if len(parts)==5 and method=='GET':return plans.overview(project_id)
                if len(parts)==6 and method=='GET':return plans.get(project_id,parts[5])
                if len(parts)==7 and method=='POST':
                    if parts[6]=='apply':return self.voice.apply_plan(project_id,parts[5])
                    if parts[6]=='choose':return plans.choose(project_id,parts[5])
                    if parts[6]=='boundary':return plans.edit_boundary(project_id,parts[5],(data or {}).get('index'),(data or {}).get('seconds'))
            if len(parts)==5 and parts[3:]==['voice','replan'] and method=='POST':return self.voice.replan(project_id,data or {})
            if len(parts)==5 and parts[3:]==['voice','montage'] and method=='POST':
                from .narration_expansion import NarrationExpansion
                return NarrationExpansion(self.voice).montage(project_id)
            if len(parts) == 5 and parts[3] == 'voice' and method == 'POST':
                return self.voice.control(project_id, parts[4])
            if len(parts) >= 4 and parts[3] == 'stock':
                service.project(project_id)
                if len(parts) == 5 and parts[4] == 'search':
                    if method == 'POST':
                        req_data = data or {}
                        return service.pixabay.search(
                            q=req_data.get('q', ''),
                            media_type=req_data.get('media_type', 'photo'),
                            orientation=req_data.get('orientation', 'horizontal'),
                            page=req_data.get('page', 1),
                            per_page=req_data.get('per_page', 20),
                            category=req_data.get('category', ''),
                            video_type=req_data.get('video_type', 'all'),
                            bypass_cache=bool(req_data.get('bypass_cache', False))
                        )
                    raise DomainError('Метод не поддерживается.', 405)
                if len(parts) == 6 and parts[4] == 'search' and method == 'GET':
                    res = service.pixabay.get_search_by_id(parts[5])
                    if not res:
                        raise DomainError('Результаты поиска не найдены.', 404)
                    return res
                if len(parts) == 5 and parts[4] == 'import':
                    if method == 'POST':
                        req_data = data or {}
                        ext_id = req_data.get('external_id') or req_data.get('id')
                        return service.pixabay.import_stock_item(
                            project_id=project_id,
                            external_id=ext_id,
                            media_type=req_data.get('media_type', 'photo')
                        )
                    raise DomainError('Метод не поддерживается.', 405)
                raise DomainError('Ресурс не найден.', 404)
            if len(parts) >= 4 and parts[3] == 'material-search':
                service.project(project_id)
                ms = service.material_search
                if len(parts) == 5 and parts[4] == 'plans' and method == 'POST':
                    return ms.create_plan(project_id, (data or {}).get('scene_ids'), (data or {}).get('options'))
                if len(parts) == 6 and parts[4] == 'plans' and method == 'GET':
                    return ms.get_plan(project_id, parts[5])
                if len(parts) == 7 and parts[4] == 'plans' and parts[6] == 'update' and method == 'POST':
                    req_data = data or {}
                    return ms.update_plan_requirement(
                        project_id, parts[5], req_data.get('requirement_id'),
                        req_data.get('user_queries'), req_data.get('requested_role'), req_data.get('enabled')
                    )
                if len(parts) == 5 and parts[4] == 'jobs' and method == 'POST':
                    return ms.start_job(project_id, (data or {}).get('plan_id'), (data or {}).get('providers'))
                if len(parts) == 6 and parts[4] == 'jobs' and method == 'GET':
                    return ms.get_job(project_id, parts[5])
                if len(parts) == 7 and parts[4] == 'jobs' and parts[6] == 'cancel' and method == 'POST':
                    return ms.cancel_job(project_id, parts[5])
                if len(parts) == 7 and parts[4] == 'jobs' and parts[6] == 'candidates' and method == 'GET':
                    return {'ok': True, 'candidates': ms.get_job_candidates(project_id, parts[5])}
                if len(parts) == 7 and parts[4] == 'candidates' and parts[6] == 'import' and method == 'POST':
                    return ms.import_candidate(project_id, parts[5])
                if len(parts) == 7 and parts[4] == 'candidates' and parts[6] == 'apply' and method == 'POST':
                    return ms.apply_candidate_to_segment(project_id, parts[5], (data or {}).get('expected_revision'))
                if len(parts) == 6 and parts[4] == 'candidates' and parts[5] == 'rollback' and method == 'POST':
                    return ms.rollback_candidate_overlay(project_id, (data or {}).get('origin_clip_ids', []), (data or {}).get('expected_revision'))
                if len(parts) == 5 and parts[4] == 'credits' and method == 'GET':
                    fmt = (data or {}).get('format', 'text')
                    return {'ok': True, 'credits': ms.export_credits(project_id, fmt)}
                if len(parts) == 5 and parts[4] == 'entities' and method == 'GET':
                    return {'ok': True, 'entities': [e.to_dict() for e in ms.planner.entities.get_project_entities(project_id)]}
                if len(parts) == 5 and parts[4] == 'image-link' and method == 'POST':
                    return ms.import_image_link(project_id, (data or {}).get('url'))
                if len(parts) == 5 and parts[4] == 'browse' and method == 'POST':
                    b_data = data or {}
                    return ms.browse_materials(
                        project_id=project_id,
                        query=b_data.get('query', ''),
                        universe_id=b_data.get('universe_id'),
                        adaptation=b_data.get('adaptation', 'any'),
                        image_type=b_data.get('image_type', 'any'),
                        filters=b_data.get('filters'),
                        provider=b_data.get('provider', 'searxng')
                    )
                if len(parts) == 6 and parts[4] == 'browse' and method == 'GET':
                    return ms.get_browse_session(project_id, parts[5])
                if len(parts) == 8 and parts[4] == 'browse' and parts[6] == 'items' and parts[7] == 'import' and method == 'POST':
                    # parts: ['api', 'projects', pid, 'material-search', 'browse', session_id, 'items', item_id, 'import'] - wait, len=9 if item_id is separate
                    pass
                if len(parts) == 9 and parts[4] == 'browse' and parts[6] == 'items' and parts[8] == 'import' and method == 'POST':
                    return ms.import_browse_item(project_id, parts[5], parts[7])
                if len(parts) == 9 and parts[4] == 'browse' and parts[6] == 'items' and parts[8] == 'apply' and method == 'POST':
                    a_data = data or {}
                    return ms.apply_browse_item_to_clip(
                        project_id=project_id,
                        session_id=parts[5],
                        item_id=parts[7],
                        clip_id=a_data.get('clip_id'),
                        expected_revision=a_data.get('expected_revision')
                    )
                if len(parts) == 9 and parts[4] == 'browse' and parts[6] == 'items' and parts[8] == 'verify' and method == 'POST':
                    v_data = data or {}
                    return ms.verify_browse_item(
                        project_id=project_id,
                        session_id=parts[5],
                        item_id=parts[7],
                        quote=v_data.get('quote')
                    )
                raise DomainError('Ресурс material-search не найден.', 404)
            if len(parts) >= 4 and parts[3] == 'edit':
                if len(parts) == 5 and parts[4] == 'media-folder':
                    if method == 'GET': return service.media_folder.latest(project_id)
                    if method == 'POST': return service.media_folder.start(project_id, data or {})
                    raise DomainError('Не найдено.', 404)
                if len(parts) >= 5 and parts[4] == 'media-library':
                    if len(parts) == 7 and parts[6] == 'clip' and method == 'POST':
                        return service.editing.library_clip(project_id, parts[5])
                    if len(parts) == 5 and method == 'GET':
                        return service.media_library.list_items(project_id)
                    if len(parts) == 6 and parts[5] == 'import' and method == 'POST':
                        folder = (data or {}).get('folder_path')
                        file_p = (data or {}).get('file_path')
                        results = []
                        if folder:
                            from pathlib import Path
                            fpath = Path(folder)
                            if fpath.is_dir():
                                for p in sorted(fpath.iterdir()):
                                    if p.is_file() and p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.mp4', '.mov', '.webm'):
                                        try:
                                            results.append(service.media_library.import_file(project_id, p))
                                        except Exception:
                                            pass
                            return {'imported': results}
                        if file_p:
                            from pathlib import Path
                            return service.media_library.import_file(project_id, Path(file_p))
                        raise DomainError('Укажите file_path или folder_path.')
                    if len(parts) == 6 and method == 'GET':
                        return service.media_library.get_item(project_id, parts[5])
                    if (len(parts) == 6 and method == 'DELETE') or (len(parts) == 7 and parts[6] == 'delete' and method == 'POST'):
                        return service.media_library.delete_item(project_id, parts[5])
                    raise DomainError('Не найдено.', 404)
                if len(parts) >= 5 and parts[4] == 'media-analysis':
                    if len(parts) == 5 and method == 'POST':
                        return service.media_analysis.start_job(project_id)
                    if len(parts) == 5 and method == 'GET':
                        job = service.media_analysis.get_active_or_latest_job(project_id)
                        return job or {'state': 'idle', 'progress': 0.0}
                    if len(parts) == 6 and parts[5] == 'cancel' and method == 'POST':
                        job = service.media_analysis.get_active_or_latest_job(project_id)
                        if job:
                            return service.media_analysis.cancel_job(project_id, job['id'])
                        return {'status': 'no_active_job'}
                    raise DomainError('Не найдено.', 404)
                if len(parts) == 5 and parts[4] == 'media-photo-motion' and method == 'POST':
                    from .insert_photo_motion import apply_to_auto_photos
                    return apply_to_auto_photos(service, project_id, data or {})
                if len(parts) >= 5 and parts[4] == 'media-match':
                    if len(parts) == 6 and parts[5] == 'progress' and method == 'GET':
                        service.project(project_id)
                        return service.media_matching.progress(project_id)
                    if len(parts) == 5 and method == 'POST':
                        return service.media_matching.propose_inserts(project_id, data or {})
                    if len(parts) == 5 and method == 'GET':
                        prop = service.db.one('SELECT * FROM media_match_proposals WHERE project_id=? ORDER BY created_at DESC LIMIT 1', (project_id,))
                        if prop:
                            prop['proposals'] = json.loads(prop.get('proposals_json') or '[]')
                        return prop or {'status': 'none', 'proposals': []}
                    if len(parts) == 7 and method == 'POST':
                        if parts[6] == 'preview':
                            return service.montage_inserts.preview_proposals(project_id, parts[5], (data or {}).get('coverage'), (data or {}).get('accepted_proposal_ids'))
                        if parts[6] == 'apply':
                            return service.montage_inserts.apply_proposals(project_id, parts[5], (data or {}).get('accepted_proposal_ids'), coverage=(data or {}).get('coverage'))
                        if parts[6] == 'rollback':
                            return service.montage_inserts.rollback_proposal(project_id, parts[5])
                    raise DomainError('Не найдено.', 404)
                if len(parts)>=5 and parts[4]=='voice-fit':
                    if len(parts)==5 and method=='POST':return self.montage_voice_fit.start(project_id,data or {})
                    if len(parts)==6 and method=='GET':return self.montage_voice_fit.get(project_id,parts[5])
                    if len(parts)==7 and method=='POST':
                        if parts[6]=='cancel':return self.montage_voice_fit.cancel(project_id,parts[5])
                        if parts[6]=='apply':return self.montage_voice_fit.apply(project_id,parts[5],data or {})
                        if parts[6]=='rollback':return self.montage_voice_fit.rollback(project_id,parts[5],data or {})
                        if parts[6]=='review':return self.montage_voice_fit.review(project_id,parts[5],data or {})
                    raise DomainError('Не найдено.',404)
                if len(parts)==5 and parts[4]=='subtitles' and method=='GET':
                    from .montage_voice_fit import subtitles
                    document=service.editing.validate(project_id,service.editing.get(project_id)['document'])
                    if document.get('voice_fit',{}).get('stale'):raise DomainError('Озвучка изменилась. Пересчитайте подгонку перед скачиванием субтитров.',409)
                    return {'srt':subtitles(document)}
                if len(parts) == 4:
                    if method == 'GET':
                        return service.editing.get(project_id)
                    if method == 'POST':
                        return service.editing.save(project_id, data)
                if len(parts) == 5 and method == 'POST':
                    if parts[4] == 'sync':
                        return service.editing.sync(project_id, data)
                    if parts[4] == 'audio':
                        return service.editing.audio(project_id, data)
                    if parts[4] == 'export':
                        return self.edit_exports.start(project_id, data)
                if len(parts) >= 6 and parts[4] == 'exports':
                    if len(parts) == 6 and method == 'GET':
                        return self.edit_exports.get(project_id, parts[5])
                    if len(parts) == 7 and parts[6] == 'cancel' and method == 'POST':
                        return self.edit_exports.cancel(project_id, parts[5])
                raise DomainError('Не найдено.', 404)
            if len(parts)==4 and parts[3]=='source-review-status' and method=='GET':
                from .source_review_status import public_reports
                return public_reports(service,project_id)
            if len(parts)==5 and parts[3:] == ['source-review-status','resume'] and method=='POST':
                from .source_review_status import public_reports
                reports=public_reports(service,project_id)['reports']
                report=next((r for r in reports if r['stage']==(data or {}).get('stage')
                    and r['run_id']==(data or {}).get('run_id') and not r['stale']),None)
                if not report or report['status']=='complete':raise DomainError('Отчёт изменился или проверка уже завершена. Обновите список.',409)
                if service.db.one("SELECT id FROM generations WHERE project_id=? AND purpose='analysis' AND state IN ('queued','preparing','running','submitted','saving') LIMIT 1",(project_id,)):
                    raise DomainError('Проверка уже выполняется.',409)
                if report['stage']=='text':
                    if service.db.one('SELECT id FROM scenes WHERE project_id=? LIMIT 1',(project_id,)):
                        raise DomainError('Сохранённые кадры не заменены. Для нового разбора используйте отдельную версию плана.',409)
                    generation=service.db.one("SELECT * FROM generations WHERE project_id=? AND purpose='analysis' ORDER BY created_at DESC LIMIT 1",(project_id,))
                    snapshot=json.loads(generation['snapshot']) if generation else {}
                    if not generation or generation['state'] not in ('failed','cancelled','resource_degraded') or not snapshot.get('pending_analysis'):
                        raise DomainError('Нет сохранённого разбора для продолжения.',409)
                    current=service.project(project_id)
                    if any(snapshot.get('project',{}).get(k)!=current[k] for k in ('id','version','story')):
                        raise DomainError('Проект изменился после разбора. Нужна отдельная версия плана.',409)
                    return {**service.retry(generation['id']),'media_regeneration_started':False}
                job=self.voice.get(project_id)
                if not job or job['state'] not in ('failed','paused') or not job['options'].get('frame_content_focus'):
                    raise DomainError('Нет приостановленного процесса проверки кадров.',409)
                options={**job['options'],'auto_render':False,'versioned_plan':True,'frames_only':True}
                self.voice.update(project_id,options_json=options)
                return self.voice.control(project_id,'resume')
            # Reading a project (including background polling) never selects
            # it for Flow. Only an explicit selection or creation may do that.
            if len(parts) == 3 and method == 'GET':
                return service.detail(project_id)
            if len(parts) == 4 and parts[3] == 'story-audit':
                from .story_audit import audit_chunk, audit_results
                if method == 'GET':
                    return {'items': audit_results(service, project_id)}
                if method == 'POST':
                    return audit_chunk(service, project_id, (data or {}).get('scene_ids'))
            if len(parts) == 4 and parts[3] == 'story-audit-regenerate' and method == 'POST':
                from .story_audit import regenerate_audit
                return regenerate_audit(service, project_id, (data or {}).get('audit_id'))
            if len(parts) == 4 and parts[3] == 'upgrade-artifact-references' and method == 'POST':
                from .story_audit import ensure_skull_reference
                return ensure_skull_reference(service, project_id)
            if len(parts) == 4 and parts[3] in ('channel-update-preview', 'channel_update_preview') and method in ('GET', 'POST'):
                return service.preview_project_channel_update(project_id, target_version_id=(data or {}).get('target_version_id'))
            if len(parts) == 4 and parts[3] in ('channel-update-apply', 'channel_update_apply') and method == 'POST':
                return service.apply_project_channel_update(project_id, target_version_id=(data or {}).get('target_version_id'))
            if len(parts) == 4 and parts[3] == 'prompts' and method == 'GET':
                params = query_params or {}
                if client == 'google-flow' and (params.get('references_only') == ['1'] or params.get('portraits_only') == ['1']):
                    return service.project_prompts(project_id, scene_ids=[])
                return service.project_prompts(project_id)
            if len(parts) == 4 and parts[3] == 'pin-participant' and method == 'POST':
                from .participant_identity import pin_participant
                return pin_participant(service, project_id, data or {})
            if len(parts) == 4 and parts[3] == 'preflight' and method in ('GET', 'POST'):
                return service.project_preflight(project_id)
            if len(parts) == 5 and parts[3:5] == ['stills', 'approve-all'] and method == 'POST':
                return service.approve_selected_stills(project_id, data or {})
            if len(parts) == 4 and parts[3] == 'cast-diff' and method == 'GET':
                return service.cast_diff(project_id)
            if len(parts) == 5 and parts[3:5] == ['cast-diff', 'resolve'] and method == 'POST':
                return service.resolve_cast_diff(project_id, data or {})
            if len(parts) == 5 and parts[3] == 'animation':
                action = parts[4]
                if action == 'recovery' and method == 'GET':
                    from .animation_recovery import manifest
                    return manifest(service, project_id)
                if action == 'prepare-direction' and method == 'POST':
                    from .animation_direction import refresh_queued_direction
                    return refresh_queued_direction(service, project_id, (data or {}).get('scene_id'), data or {})
                if action == 'selection' and method == 'POST':
                    return service.apply_animation_selection(project_id,data or {})
                if action == 'plan' and method == 'POST':
                    return service.build_animation_plan(project_id, (data or {}).get('plan_mode'))
                if action == 'generate' and method == 'POST':
                    return service.start_animation_generation(project_id, scene_ids=(data or {}).get('scene_ids'))
                if action == 'regenerate-local' and method == 'POST':
                    return service.start_animation_generation(project_id, scene_ids=(data or {}).get('scene_ids'), regenerate_local=True)
                if action == 'status' and method == 'GET':
                    return service.animation_status(project_id)
                if action == 'jobs' and method == 'GET':
                    result = service.animation_jobs(project_id)
                    # Old adapters only filter the image loop of a scoped start.
                    # Apply that scope to their video fetch as well.
                    if client == 'google-flow':
                        with self.flow_bridge_v1_lock:
                            bridge = self.flow_bridge_v1
                            scope = bridge.get('animation_scene_ids')
                            if scope is not None and (bridge['project_id'] != project_id or time.time() - bridge['last_seen'] >= BRIDGE_ADAPTER_TTL):
                                raise DomainError('Связь Flow изменилась. Повторите запуск выбранных кадров.', 409)
                        if scope is not None:
                            result = {**result, 'jobs': [job for job in result['jobs'] if job['scene_id'] in scope]}
                    return result
                if action == 'assemble' and method == 'POST':
                    return service.assemble_animation_timeline(project_id, (data or {}).get('transition_seconds', 0.3))
            if len(parts) == 4 and method == 'POST':
                action = parts[3]
                if action == 'import':
                    return service.import_analysis(project_id, data['analysis'], data['version'])
                if action == 'analyse':
                    if any(k in data for k in ('camera_mode', 'camera_pacing', 'aspect_ratio')):
                        service.update_camera_settings(project_id, data)
                    project = service.project(project_id)
                    target = data.get('target', 12)
                    if type(target) is not int or not 1 <= target <= MAX_SCENES:
                        raise DomainError(f'Для автоматического анализа выберите 1–{MAX_SCENES} сцен.')
                    rebuild = bool((data or {}).get('rebuild'))
                    with service.db.transaction() as connection:
                        if connection.execute("SELECT id FROM generations WHERE project_id=? AND purpose='analysis' AND state='waiting_review' LIMIT 1", (project_id,)).fetchone():
                            raise DomainError('Готовый разбор ожидает проверки. Отмените его перед новым анализом.', 409)
                        has_scenes = connection.execute('SELECT id FROM scenes WHERE project_id=? LIMIT 1', (project_id,)).fetchone()
                        if has_scenes and rebuild:
                            service._require_unreviewed_analysis(connection, project_id)
                        if has_scenes and not rebuild:
                            raise DomainError('Разбор уже сохранён. Редактируйте сцены или создайте новый проект.', 409)
                        if rebuild and connection.execute(
                            "SELECT id FROM generations WHERE project_id=? AND purpose NOT IN ('analysis','edit_audio') LIMIT 1",
                            (project_id,),
                        ).fetchone():
                            raise DomainError('Пересборка недоступна после создания изображений или видео. Создайте новый проект.', 409)
                        settings = service.settings()
                        generation_id = service.insert_generation(connection, project_id, 'analysis',
                            {'profile': settings, 'project': project, 'target': target, 'story_analyzer_version': 'deepseek',
                             'analyzer_version': 'deepseek', 'analysis_rebuild': rebuild})
                    return {'generation_id': generation_id}
                if action == 'batch':
                    return service.batch(project_id, data['idempotency_key'], data.get('scene_ids'))
                if action == 'accept-references':
                    return service.accept_ready_references(project_id)
        if len(parts) == 4 and parts[:2] == ['api', 'characters'] and method == 'GET' and parts[3] == 'prompt':
            character = service.character(parts[2])
            return {'prompt': service.casting_snapshot(service.project(character['project_id']), character)['prompt']}
        if len(parts) == 4 and parts[:2] == ['api', 'characters'] and method == 'POST':
            character_id, action = parts[2:]
            if action == 'cast':
                return service.cast(character_id, data.get('count', 1), data.get('reference_asset'), data.get('wardrobe_key'))
            if action == 'cast-failed':
                return service.fail_character_cast(character_id,data.get('generation_id'),data.get('request_id'),data.get('reason'))
            if action == 'cast-start':
                return service.claim_character_cast(character_id,data.get('generation_id'),data.get('request_id'))
            if action == 'lock':
                return service.lock(character_id, data['references'], data['revision'], data.get('review'))
            if action == 'unlock':
                return service.unlock_character(character_id)
            if action == 'edit':
                return service.edit_character(character_id, data['description'], data['revision'], name=data.get('name'))
            if action == 'upload':
                return service.upload_character_portrait(character_id, extract_image_bytes(data), lock=bool(data.get('lock', True)),generation_id=data.get('generation_id'),request_id=data.get('request_id'))
        if len(parts) == 4 and parts[:2] == ['api', 'locations'] and method == 'POST':
            location_id, action = parts[2:]
            if action == 'cast':
                return service.cast_location(location_id, data.get('count', 2))
            if action == 'select':
                return service.select_reference('location', location_id, data['asset_id'], data['revision'])
            if action == 'lock':
                return service.lock_location(location_id, data['references'], data['revision'])
            if action == 'unlock':
                return service.unlock_location(location_id)
            if action == 'edit':
                return service.edit_location(location_id, data['description'], data['revision'])
            if action == 'upload':
                return service.upload_location_image(location_id, extract_image_bytes(data), lock=bool(data.get('lock', True)))
        if len(parts) == 4 and parts[:2] == ['api', 'reference_jobs'] and parts[3] == 'upload' and method == 'POST':
            job = service.db.one('SELECT * FROM generations WHERE id=?', (parts[2],))
            if not job or job['purpose'] not in ('location', 'prop'):
                raise DomainError('Задача кастинга не найдена.', 404)
            if job['state'] == 'succeeded':
                return {'asset_id': job['asset_id']}
            kind = job['purpose']
            target = getattr(service, kind)(job[f'{kind}_id'])
            session = service.reference_cast(kind, target)
            if (job['state'] != 'queued' or job['cancel_requested'] or target['locked']
                    or target['revision'] != job[f'{kind}_revision']
                    or not session or job['id'] not in session['generation_ids']):
                raise DomainError('Кастинг отменён, закреплён или изменён.', 409)
            profile = json.loads(job['snapshot'])['profile']
            image = service._process_image_bytes(extract_image_bytes(data), (profile['width'], profile['height']))
            self.worker.save_image(job, image)
            return {'asset_id': service.db.one('SELECT asset_id FROM generations WHERE id=?', (job['id'],))['asset_id']}
        if len(parts) == 4 and parts[:2] == ['api', 'props'] and method == 'POST':
            prop_id, action = parts[2:]
            if action == 'cast':
                return service.cast_prop(prop_id, data.get('count', 2))
            if action == 'select':
                return service.select_reference('prop', prop_id, data['asset_id'], data['revision'])
            if action == 'lock':
                return service.lock_prop(prop_id, data['references'], data['revision'])
            if action == 'unlock':
                return service.unlock_prop(prop_id)
            if action == 'edit':
                return service.edit_prop(prop_id, data['description'], data['revision'], importance=data.get('importance'))
            if action == 'upload':
                return service.upload_prop_image(prop_id, extract_image_bytes(data), lock=bool(data.get('lock', True)))
        if len(parts)==4 and parts[:2]==['api','scenes'] and parts[3]=='composition':
            from .frame_composition import inspect,save
            if method=='GET':return inspect(service,parts[2])
            if method=='POST':return save(service,parts[2],data)
        if len(parts) in (4,5) and parts[:2] == ['api','scenes'] and parts[3] == 'editorial-plans':
            from .editorial_plans import EditorialPlans
            plans = EditorialPlans(service)
            if len(parts)==5 and parts[4]=='choices' and method in ('GET','POST'):return plans.choices(parts[2], data.get('focus') if method=='POST' else None)
            if len(parts)==4 and method=='GET':return {'plans': plans.overview(parts[2])}
            if len(parts)==4 and method=='POST':return plans.preview(parts[2], data.get('decision'), data.get('revision'))
            if len(parts)==5 and parts[4]=='apply' and method=='POST':return plans.apply(parts[2], data.get('plan_id'), data.get('revision'))
        if len(parts) == 4 and parts[:2] == ['api', 'scenes'] and method == 'POST':
            if parts[3] == 'edit':
                return service.edit_scene(parts[2], data['data'], data['revision'])
            if parts[3] == 'prompt':
                return service.scene_snapshot(service.scene(parts[2]), allow_unlocked=True)
            if parts[3] == 'upload':
                expected=data.get('expected_scene_revision')
                if expected is not None and (type(expected) is not int or service.scene(parts[2])['current_revision']!=expected):
                    raise DomainError('Кадр изменился после запуска Flow. Результат сохранён в мосте.',409)
                request_id=data.get('admission_request_id')
                admission=None
                if request_id:
                    from .flow_admission import FlowAdmission
                    admission=FlowAdmission(service);admission.validate(parts[2],request_id)
                try:
                    result=service.upload_scene_image(parts[2],extract_image_bytes(data),image_validator=self.worker.validate_external_scene_image,
                        fidelity_candidate_id=data.get('fidelity_candidate_id'))
                except DomainError as error:
                    if admission and error.status==422:admission.finish(parts[2],request_id,'rejected')
                    raise
                if admission:admission.finish(parts[2],request_id,'uploaded')
                return result
            if parts[3] == 'video':
                flow_media_id = (data or {}).get('flow_media_id') if isinstance(data, dict) else None
                idempotency_key = (data or {}).get('idempotency_key') if isinstance(data, dict) else None
                fn = (data or {}).get('filename', 'video.mp4') if isinstance(data, dict) else 'video.mp4'
                source = (data or {}).get('source', 'auto') if isinstance(data, dict) else 'auto'
                rev = (data or {}).get('expected_scene_revision') if isinstance(data, dict) else None
                source_img = (data or {}).get('source_image_asset_id') if isinstance(data, dict) else None
                source_sha = (data or {}).get('source_image_sha256') if isinstance(data, dict) else None
                return service.upload_scene_video(
                    parts[2], extract_image_bytes(data), filename=fn,
                    flow_media_id=flow_media_id, idempotency_key=idempotency_key,
                    source=source, expected_scene_revision=rev, source_image_asset_id=source_img,
                    source_image_sha256=source_sha
                )
            if parts[3] == 'delete_video' and method in ('POST', 'DELETE', 'GET'):
                return service.delete_scene_video(parts[2])
            if parts[3] == 'video' and method == 'DELETE':
                return service.delete_scene_video(parts[2])
        if len(parts) == 4 and parts[:2] == ['api', 'scenes'] and method in ('DELETE', 'GET'):
            if parts[3] == 'delete_video':
                return service.delete_scene_video(parts[2])
            if parts[3] == 'video' and method == 'DELETE':
                return service.delete_scene_video(parts[2])
        if len(parts) == 5 and parts[:2] == ['api', 'scenes'] and parts[3:5] == ['video', 'import'] and method == 'POST':
            idempotency_key = (data or {}).get('idempotency_key') if isinstance(data, dict) else None
            fn = (data or {}).get('filename', 'video.mp4') if isinstance(data, dict) else 'video.mp4'
            rev = (data or {}).get('expected_scene_revision') if isinstance(data, dict) else None
            source_img = (data or {}).get('source_image_asset_id') if isinstance(data, dict) else None
            source_sha = (data or {}).get('source_image_sha256') if isinstance(data, dict) else None
            source = (data or {}).get('source', 'flow_manual') if isinstance(data, dict) else 'flow_manual'
            return service.import_scene_video(
                parts[2], extract_image_bytes(data), filename=fn,
                expected_scene_revision=rev, source_image_asset_id=source_img,
                source_image_sha256=source_sha,
                idempotency_key=idempotency_key, source=source
            )
        if len(parts) == 4 and parts[:2] == ['api', 'scenes'] and parts[3] == 'composite-reference' and method == 'GET':
            return service.scene_composite_reference(parts[2])
        if len(parts) == 5 and parts[:2] == ['api', 'scenes'] and parts[3] == 'animation':
            if parts[4] == 'passport' and method == 'GET':
                return service.animation_passport(parts[2])
            if method == 'POST':
                if parts[4] == 'resolve':
                    from .animation_recovery import resolve_no_result
                    return resolve_no_result(service, parts[2], data or {})
                if parts[4] == 'select':
                    return service.select_scene_animation_engine(
                        parts[2],
                        (data or {}).get('engine', 'veo_lite'),
                        camera_motion=(data or {}).get('camera_motion'),
                        duration_seconds=(data or {}).get('duration_seconds'),
                        ken_burns_preset=(data or {}).get('ken_burns_preset'),
                        omni_resolution=(data or {}).get('omni_resolution'),
                        motion_scope=(data or {}).get('motion_scope')
                    )
                if parts[4] == 'queue_veo':
                    return service.queue_single_scene_veo(
                        parts[2],
                        camera_motion=(data or {}).get('camera_motion')
                    )
                if parts[4] == 'queue_omni':
                    return service.queue_single_scene_omni(
                        parts[2],
                        duration_seconds=(data or {}).get('duration_seconds') or 6,
                        resolution=(data or {}).get('resolution') or '720p'
                    )
                if parts[4] == 'retry':
                    return service.retry_scene_animation(parts[2])
                if parts[4] == 'delete':
                    return service.delete_scene_video(parts[2])
                if parts[4] == 'progress':
                    return service.set_scene_animation_status(parts[2], (data or {}).get('status', 'generating_veo'), (data or {}).get('error'),
                        expected_scene_revision=(data or {}).get('expected_scene_revision'),
                        idempotency_key=(data or {}).get('idempotency_key'),
                        source_image_asset_id=(data or {}).get('source_image_asset_id'),
                        source_image_sha256=(data or {}).get('source_image_sha256'))
        if len(parts) == 4 and parts[:2] == ['api', 'batches'] and method == 'POST':
            return service.control_batch(parts[2], parts[3])
        if len(parts) == 4 and parts[:2] == ['api', 'generations'] and method == 'POST':
            if parts[3] == 'select':
                return service.select_generation(parts[2], data.get('note', ''))
            if parts[3] == 'review':
                return service.review_generation(
                    parts[2], (data or {}).get('decision'),
                    reason_code=(data or {}).get('reason_code', ''),
                    note=(data or {}).get('note', '')
                )
            if parts[3] == 'retry':
                return service.retry(parts[2])
            if parts[3] == 'cancel':
                return service.cancel_generation(parts[2])
        raise DomainError('Маршрут не найден.', 404)


def make_server(app, port=8765):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def handle(self):
            try:
                super().handle()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                log.info('HTTP client disconnected')

        def log_message(self, format, *args):
            log.debug(format, *args)

        def send_body(self, status, body, content_type='application/json; charset=utf-8', extra=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Session-Token, X-Client, X-File-Name')
            self.send_header('Content-Security-Policy', "default-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data: https://*.pixabay.com https://pixabay.com https://*.vimeocdn.com https://*.wikimedia.org https://commons.wikimedia.org https://upload.wikimedia.org https://*.wikipedia.org; media-src 'self' blob: data: https://*.pixabay.com https://pixabay.com; frame-ancestors 'none'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            try:
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                log.info('HTTP client disconnected during send_body')

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Session-Token, X-Client, X-File-Name')
            self.send_header('Access-Control-Max-Age', '86400')
            self.end_headers()

        def do_GET(self):
            self.handle_request('GET')

        def do_POST(self):
            self.handle_request('POST')

        def do_DELETE(self):
            self.handle_request('DELETE')

        def handle_request(self, method):
            try:
                allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}', '127.0.0.1', 'localhost'}
                host = self.headers.get('Host', '')
                if host not in allowed and not host.startswith('127.0.0.1') and not host.startswith('localhost'):
                    raise DomainError('Недопустимый Host.', 403)
                origin = self.headers.get('Origin')
                if origin and origin not in {'http://' + value for value in allowed} and not origin.startswith('https://flow.google.com') and not origin.startswith('chrome-extension://') and not origin.startswith('moz-extension://') and origin != 'null':
                    raise DomainError('Доступ с другого сайта запрещён.', 403)
                parsed = urlparse(self.path)
                path = parsed.path
                query_params = parse_qs(parsed.query)
                data = {}
                if method == 'POST' and path == '/api/shutdown':
                    if self.headers.get('X-Session-Token') != app.token:
                        raise DomainError('Недействительная сессия.', 403)
                    self.send_body(200, json.dumps({'status': 'shutting_down'}).encode())
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return

                if method == 'POST':
                    is_flow = (
                        (origin and origin.startswith('https://flow.google.com')) or
                        self.headers.get('X-Client') == 'google-flow' or
                        path.endswith('/upload') or
                        path.endswith('/video') or
                        path.endswith('/video/import') or
                        path.startswith('/api/flow/') or
                        path.startswith('/api/bridge/')
                    )
                    if '/edit' in path:
                        is_flow = False
                    if self.headers.get('X-Session-Token') != app.token and not is_flow:
                        raise DomainError('Недействительная сессия. Перезагрузите страницу.', 403)
                    length = int(self.headers.get('Content-Length', '0'))
                    if path.startswith('/api/projects/') and path.endswith('/edit/media-library/upload'):
                        import tempfile
                        from urllib.parse import unquote
                        parts = path.strip('/').split('/')
                        project_id = parts[2]
                        if len(parts) != 6 or not 0 < length <= 512_000_000:
                            raise DomainError('Файл должен быть не больше 512 МБ.', 413)
                        if self.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream':
                            raise DomainError('Ожидается медиафайл.', 415)
                        app.service.project(project_id)
                        upload_dir = app.service.root / 'outputs' / 'media_library' / 'tmp'
                        upload_dir.mkdir(parents=True, exist_ok=True)
                        filename = unquote(self.headers.get('X-File-Name', 'media_file'))
                        with tempfile.NamedTemporaryFile(dir=upload_dir, suffix=Path(filename).suffix, delete=False) as upload:
                            temporary = Path(upload.name)
                        try:
                            with temporary.open('wb') as upload:
                                remaining = length
                                while remaining:
                                    block = self.rfile.read(min(remaining, 1024*1024))
                                    if not block:
                                        raise DomainError('Загрузка файла прервалась.')
                                    upload.write(block)
                                    remaining -= len(block)
                            result = app.service.media_library.import_file(project_id, temporary, filename)
                        finally:
                            temporary.unlink(missing_ok=True)
                        self.send_body(200, json.dumps(result, ensure_ascii=False).encode('utf-8'))
                        return
                    if path.startswith('/api/projects/') and path.endswith('/edit/video'):
                        import tempfile
                        from urllib.parse import unquote
                        parts = path.strip('/').split('/')
                        if len(parts) != 5 or not 0 < length <= 512_000_000:
                            raise DomainError('Видеофайл должен быть не больше 512 МБ.', 413)
                        if self.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream':
                            raise DomainError('Ожидается видеофайл.', 415)
                        app.service.editing.project(parts[2])
                        upload_dir = app.service.root/'edit_uploads'
                        upload_dir.mkdir(exist_ok=True)
                        filename = unquote(self.headers.get('X-File-Name', 'footage.mp4'))
                        with tempfile.NamedTemporaryFile(dir=upload_dir, suffix='.upload', delete=False) as upload:
                            temporary = Path(upload.name)
                        try:
                            with temporary.open('wb') as upload:
                                remaining = length
                                while remaining:
                                    block = self.rfile.read(min(remaining, 1024*1024))
                                    if not block:
                                        raise DomainError('Загрузка видео прервалась.')
                                    upload.write(block)
                                    remaining -= len(block)
                            result = app.service.editing.import_video_file(parts[2], temporary, filename)
                        finally:
                            temporary.unlink(missing_ok=True)
                        self.send_body(200, json.dumps(result, ensure_ascii=False).encode('utf-8'))
                        return
                    max_len = 150_000_000 if ('/video' in path or '/upload' in path or path.endswith('/edit/audio')) else 25_000_000
                    if length > max_len or length < 0:
                        raise DomainError('Недопустимый размер запроса.', 413)
                    if length > 0:
                        data = json.loads(self.rfile.read(length))
                        if not isinstance(data, dict):
                            raise DomainError('Тело запроса должно быть объектом.')
                    else:
                        data = {}
                if method == 'GET' and '/material-search/browse/' in path and path.endswith('/preview'):
                    req_parts = path.strip('/').split('/')
                    if len(req_parts) == 9 and req_parts[0] == 'api' and req_parts[1] == 'projects' and req_parts[3] == 'material-search' and req_parts[4] == 'browse' and req_parts[6] == 'items' and req_parts[8] == 'preview':
                        project_id = req_parts[2]
                        session_id = req_parts[5]
                        item_id = req_parts[7]
                        prev = app.service.material_search.get_browse_item_preview(project_id, session_id, item_id)
                        self.send_body(200, prev['bytes'], prev.get('content_type', 'image/jpeg'))
                        return
                if method == 'GET' and path.startswith('/api/channel_references/'):
                    asset_id = path.split('/')[-1]
                    ref = app.service.db.one('SELECT file_path FROM channel_reference_assets WHERE id=?', (asset_id,))
                    if not ref:
                        raise DomainError('Файл референса не найден.', 404)
                    target_file = (app.service.root / ref['file_path']).resolve()
                    if not target_file.is_file():
                        raise DomainError('Файл референса отсутствует на диске.', 404)
                    mime, _ = mimetypes.guess_type(str(target_file))
                    self.send_body(200, target_file.read_bytes(), mime or 'image/png')
                    return
                if method == 'GET' and path.startswith('/data/'):
                    clean = path.lstrip('/')
                    target_file = (app.service.root / clean).resolve()
                    if target_file.is_relative_to(app.service.root.resolve()) and target_file.is_file():
                        mime, _ = mimetypes.guess_type(str(target_file))
                        self.send_body(200, target_file.read_bytes(), mime or 'application/octet-stream')
                        return
                if method == 'GET' and path.startswith('/api/media-file/'):
                    from urllib.parse import unquote
                    subpath = unquote(path[len('/api/media-file/'):])
                    target_file = (app.service.root / subpath).resolve()
                    if not target_file.is_relative_to((app.service.root / 'outputs' / 'media_library').resolve()) or not target_file.is_file():
                        raise DomainError('Файл не найден.', 404)
                    mime, _ = mimetypes.guess_type(str(target_file))
                    mime = mime or 'application/octet-stream'
                    if mime.startswith('video/') and query_params.get('download') != ['1']:
                        from .browser_media import playable_path
                        target_file=playable_path(target_file,app.service.root/'outputs'/'browser_media')
                        mime='video/mp4'
                    size = target_file.stat().st_size
                    start, end, status = 0, size - 1, 200
                    request_range = self.headers.get('Range')
                    if request_range:
                        import re
                        match = re.fullmatch(r'bytes=(\d*)-(\d*)', request_range)
                        if match and any(match.groups()):
                            lo, hi = match.groups()
                            start = int(lo) if lo else max(0, size - int(hi))
                            end = min(int(hi), size - 1) if hi and lo else size - 1
                            if start < size and end >= start:
                                status = 206
                    if request_range and status != 206:
                        self.send_body(416, b'Invalid range', 'text/plain')
                        return
                    self.send_response(status)
                    self.send_header('Content-Type', mime)
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    self.send_header('Content-Length', str(end - start + 1))
                    self.send_header('Cache-Control', 'public, max-age=31536000, immutable')
                    self.end_headers()
                    with open(target_file, 'rb') as f:
                        f.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            chunk = f.read(min(remaining, 65536))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    return
                if method == 'GET' and path.startswith('/api/assets/'):
                    asset = app.service.asset(path.split('/')[-1])
                    mime, _ = mimetypes.guess_type(asset['path'])
                    if (mime or '').startswith(('video/', 'audio/')):
                        file = app.service.root / asset['path']
                        if (mime or '').startswith('video/') and query_params.get('download') != ['1']:
                            from .browser_media import playable_path
                            file=playable_path(file,app.service.root/'outputs'/'browser_media')
                            mime='video/mp4'
                        size = file.stat().st_size
                        start, end, status = 0, size - 1, 200
                        request_range = self.headers.get('Range')
                        if request_range:
                            import re
                            match = re.fullmatch(r'bytes=(\d*)-(\d*)', request_range)
                            try:
                                if not match or not any(match.groups()):
                                    raise ValueError()
                                lo, hi = match.groups()
                                if lo:
                                    start, end = int(lo), min(int(hi), size-1) if hi else size-1
                                else:
                                    count = int(hi)
                                    if count <= 0:
                                        raise ValueError()
                                    start = max(0, size-count)
                                if start >= size or end < start:
                                    raise ValueError()
                                status = 206
                            except ValueError:
                                self.send_body(416, b'', mime, {'Content-Range':f'bytes */{size}', 'Accept-Ranges':'bytes'})
                                return
                        self.send_response(status)
                        self.send_header('Content-Type', mime)
                        self.send_header('Content-Length', str(end-start+1))
                        self.send_header('Accept-Ranges', 'bytes')
                        if status == 206:
                            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        if query_params.get('download') == ['1']:
                            self.send_header('Content-Disposition', 'attachment; filename="frameforge.mp4"')
                        self.end_headers()
                        with file.open('rb') as source:
                            source.seek(start)
                            remaining = end-start+1
                            while remaining:
                                chunk = source.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                        return
                    self.send_body(200, (app.service.root / asset['path']).read_bytes(), mime or 'image/png')
                    return
                if method == 'GET' and path.startswith('/api/export/'):
                    body = app.service.export(path.split('/')[-1])
                    self.send_body(200, body, 'application/zip', {'Content-Disposition': 'attachment; filename="images.zip"'})
                    return
                if path.startswith('/api/'):
                    # Bridge messages have their own lock. A long local render
                    # must never keep heartbeats or pause/start commands waiting.
                    if method == 'POST' and not path.startswith('/api/bridge/v1/') and not path.endswith('/story-audit') and '/edit' not in path:
                        with app.mutation_lock:
                            result = app.dispatch(method, path, data, query_params=query_params, client=self.headers.get('X-Client'))
                    else:
                        result = app.dispatch(method, path, data, query_params=query_params, client=self.headers.get('X-Client'))
                    self.send_body(200, dump(result).encode())
                    return
                if method == 'GET':
                    clean_path = path.lstrip('/')
                    if clean_path.startswith('static/'):
                        clean_path = clean_path[len('static/'):]
                    relative = 'index.html' if not clean_path else clean_path
                    file = (STATIC / relative).resolve()
                    if not file.is_file():
                        file = (STATIC / path.lstrip('/')).resolve()
                    if file.is_relative_to(STATIC.resolve()) and file.is_file():
                        mime = mimetypes.guess_type(file)[0] or 'application/octet-stream'
                        # .user.js files: no CSP, correct content-type for Tampermonkey
                        if str(file).endswith('.user.js'):
                            body = file.read_bytes()
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/javascript; charset=utf-8')
                            self.send_header('Content-Length', str(len(body)))
                            self.send_header('Cache-Control', 'no-store')
                            self.send_header('Access-Control-Allow-Origin', '*')
                            self.send_header('Access-Control-Allow-Private-Network', 'true')
                            self.end_headers()
                            self.wfile.write(body)
                        else:
                            self.send_body(200, file.read_bytes(), mime)
                        return
                raise DomainError('Не найдено.', 404)
            except DomainError as error:
                self.send_body(error.status, dump({'error': str(error), **getattr(error, 'details', {})}).encode())
            except ProviderError as error:
                self.send_body(503, dump({'error': str(error)}).encode())
            except (ValueError, KeyError, TypeError) as error:
                self.send_body(400, dump({'error': f'Некорректный запрос: {error}'}).encode())
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                log.info('HTTP client disconnected')
            except Exception:
                log.exception('HTTP request failed')
                self.send_body(500, dump({'error': 'Внутренняя ошибка; подробности в журнале сервера.'}).encode())

    class SafeServer(ThreadingHTTPServer):
        allow_reuse_address = False
        daemon_threads = True

        def handle_error(self, request, client_address):
            import sys
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
                log.info('Client disconnected: %s', client_address)
                return
            super().handle_error(request, client_address)

    server = SafeServer(('127.0.0.1', port), Handler)
    return server


if __name__ == '__main__':
    from .__main__ import main
    main()
