"""Phrase timing for a separate montage proposal. No scene/generation writes."""
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from .db import uid
from .domain import DomainError
from .editing import normalize

VERSION='montage-voice-fit/1'
ACOUSTIC={'exact','fuzzy','compound','normalized_group'}


def signature(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def source_signature(document):
    return signature([{k:c.get(k) for k in ('edit_clip_id','scene_id','source_scene_revision',
        'source_video_asset_id','source_image_asset_id','media_type','camera_motion')}
        for c in document['clips']])


def audio_binding(service,pid,doc):
    tracks=[a for a in doc.get('audio_tracks',[]) if a['role']=='voice' and a.get('enabled',True)]
    if len(tracks)!=1:return None
    a=tracks[0];asset=service.asset(a['asset_id'])
    if asset['project_id']!=pid:raise DomainError('Озвучка не принадлежит проекту.')
    return {'asset_id':asset['id'],'sha256':asset['sha256'],
            'trim_in_ms':a['trim_in_ms'],'trim_out_ms':a['trim_out_ms'],'offset_ms':a.get('offset_ms',0)}


def source_rows(service,pid):
    rows=service.db.all('''SELECT s.id,s.position,s.current_revision,s.video_asset_id,
        s.active_generation_id,r.data,g.asset_id AS image_asset_id,a.width AS image_width,a.height AS image_height
        FROM scenes s JOIN scene_revisions r ON r.scene_id=s.id AND r.revision=s.current_revision
        LEFT JOIN generations g ON g.id=s.active_generation_id LEFT JOIN assets a ON a.id=g.asset_id
        WHERE s.project_id=? ORDER BY s.position''',(pid,))
    return [{**r,'data':json.loads(r['data'])} for r in rows]


def snapshot(service,pid,edit):
    project=service.project(pid)
    return {'project_id':pid,'project_version':project['version'],'story':project['story'],
            'scenes':source_rows(service,pid),'edit_revision':edit['revision'],
            'document':copy.deepcopy(edit['document']),
            'audio_binding':audio_binding(service,pid,edit['document'])}


def propose(snap,alignment,policy='hold'):
    """Pure calculation: exact quote coverage, one boundary clock, explicit conflicts."""
    if policy not in ('hold','speed','error'):raise DomainError('Выберите правило для короткого видео.')
    story=snap['story'];doc=copy.deepcopy(snap['document']);voice=snap['audio_binding']
    if not voice:raise DomainError('Добавьте и включите озвучку в монтаже.')
    words=alignment['words'];scenes={s['id']:s for s in snap['scenes']}
    conflicts=[];warnings=[];changes=[]
    def conflict(code,message,clip=None):
        conflicts.append({'code':code,'message':message,'clip_id':clip})
    if alignment.get('story_sha256')!=hashlib.sha256(story.encode()).hexdigest():
        raise DomainError('Сопоставление относится к другому сценарию.')
    if alignment.get('metrics',{}).get('backwards_word_starts'):
        conflict('nonmonotonic_words','Время слов идёт в обратном порядке; нужна проверка записи.')
    track=next(a for a in doc['audio_tracks'] if a['role']=='voice')
    audio_start=voice['trim_in_ms'];audio_end=voice['trim_out_ms'];offset=voice['offset_ms']
    if abs(alignment['audio_duration']*1000-track.get('source_duration_ms',audio_end))>100:
        conflict('audio_duration_changed','Длительность распознанной записи отличается от текущего файла.')
    if audio_end<=audio_start:raise DomainError('Пустой интервал озвучки.')
    active=[c for c in doc['clips'] if not c.get('excluded')]
    if not active:conflict('no_clips','Добавьте готовые сцены в монтаж перед подгонкой.')
    old={c['edit_clip_id']:c for c in normalize(doc)['clips']}
    spans=[];ranges=[]
    for c in active:
        cid=c['edit_clip_id'];scene=scenes.get(c.get('scene_id'));data=(scene or {}).get('data',{})
        proof=c.get('voice_source_binding') or {k:data.get('source_'+k) for k in ('start','end','quote')}
        if c.get('source_kind')=='footage' and not c.get('voice_source_binding'):
            conflict('unbound_footage','Футаж требует ручной привязки к точной цитате.',cid);spans.append(None);ranges.append([]);continue
        a,b=proof.get('start'),proof.get('end')
        if type(a) is not int or type(b) is not int or not 0<=a<b<=len(story) or story[a:b]!=proof.get('quote'):
            conflict('unbound_quote','Нет подтверждённой точной цитаты клипа.',cid);spans.append(None);ranges.append([]);continue
        spans.append({'start':a,'end':b,'quote':story[a:b]})
        selected=[w for w in words if a<=w['char_start']<w['char_end']<=b]
        ranges.append(selected)
        if not selected:conflict('empty_phrase','В цитате не найдены произносимые слова.',cid)
        if scene and c.get('source_scene_revision')!=scene['current_revision']:
            conflict('stale_scene','Сцена изменена после добавления клипа; обновите источник.',cid)
    # Every spoken source word belongs to exactly one included quote. A repeated
    # scene or incomplete montage is a visible conflict, never an invented split.
    relevant=[w for w in words if w.get('start') is None or w.get('end') is None
              or (w['end']*1000>audio_start and w['start']*1000<audio_end)]
    missing=[w for w in relevant if sum(bool(s and s['start']<=w['char_start']<w['char_end']<=s['end']) for s in spans)!=1]
    if missing:
        conflict('source_coverage','Включённые клипы пропускают или повторяют текст: '+
            ' '.join(w['text'] for w in missing[:12]))
    for left,right in zip(spans,spans[1:]):
        if left and right and left['end']>right['start']:
            conflict('manual_order','Порядок или перекрытие цитат не соответствует речи. Порядок клипов сохранён.')
    boundaries=[0.0]
    for index,selected in enumerate(ranges):
        cid=active[index]['edit_clip_id']
        if not selected:
            boundaries.append(boundaries[-1]);continue
        poor=[w for w in selected if w.get('method') not in ACOUSTIC or w.get('start') is None or w.get('end') is None
              or w.get('number_mismatch') or w.get('numeric_unresolved')]
        if poor:conflict('uncertain_phrase','Часть слов не подтверждена записью: '+ ' '.join(w['text'] for w in poor[:6]),cid)
        if any(w.get('end') is not None and (w['end']*1000<=audio_start or w['start']*1000>=audio_end) for w in selected):
            conflict('phrase_outside_audio','Часть цитаты находится за обрезанной озвучкой. Привяжите клип к оставшейся фразе вручную.',cid)
        if selected[0].get('confidence',1)<.55:
            conflict('uncertain_boundary','Низкая уверенность в начале фразы; проверьте границу.',cid)
        if index:
            word=selected[0]
            if word.get('timing_group') and spans[index]['start']>word['timing_group']['char_start']:
                conflict('number_boundary','Граница делит числительное с общим временем.',cid)
            t=word.get('start')
            boundaries[index]=offset+max(0,(t or 0)*1000-audio_start)
        boundaries.append(0.0)
    if active:boundaries[-1]=offset+audio_end-audio_start
    cues=[]
    for i,c in enumerate(active):
        cid=c['edit_clip_id'];start,end=boundaries[i:i+2];duration=end-start
        if duration<1000/24:
            conflict('short_interval','Интервал короче одного видеокадра; нужна ручная граница.',cid);continue
        previous=old[cid];media=c.get('media_type','video')
        fitted=c.get('voice_fit_auto')
        manual=c.get('timing_locked') or c.get('manual_timing') or (
            not fitted and not c.get('allow_voice_retime') and media!='image' and
            (c['trim_in_ms']>0 or c['trim_out_ms']<c['source_duration_ms']-.1 or c.get('playback_rate',1)!=1 or c.get('hold_last_ms',0)))
        if manual and (abs(previous['start_ms']-start)>.01 or abs(previous.get('base_end_ms',previous['end_ms'])-end)>.01):
            conflict('manual_boundary','Ручное или закреплённое время не совпадает с фразой. Оно сохранено; снимите фиксацию для новой подгонки.',cid)
            continue
        if c.get('timing_locked'):
            if spans[i]:cues.append({'clip_id':cid,'start_ms':start,'end_ms':end,'quote':spans[i]['quote']})
            continue
        scene=scenes.get(c.get('scene_id'),{});animation=scene.get('data',{}).get('animation',{})
        if (media!='image' and animation.get('source')=='ken_burns'
                and scene.get('image_asset_id')==c.get('source_image_asset_id')
                and scene.get('image_width') and scene.get('image_height')):
            from .montage_media import camera_plan
            c['original_video_asset_id']=c.get('source_video_asset_id')
            c.update(media_type='image',source_video_asset_id=None,source_duration_ms=86400000,
                camera_motion=camera_plan(scene['image_width'],scene['image_height'],animation.get('ken_burns_preset') or 'gentle_push_in'))
            media='image'
            warnings.append({'clip_id':cid,'code':'camera_retimed','message':'Движение 2.5D рассчитано на время фразы из исходного изображения.'})
        if media=='image':
            c.update(trim_in_ms=0,trim_out_ms=duration,playback_rate=1,hold_last_ms=0,muted=True)
        else:
            available=c['source_duration_ms']-c['trim_in_ms']
            c['playback_rate']=1;c['hold_last_ms']=0
            if available>=duration:
                c['trim_out_ms']=c['trim_in_ms']+duration
            elif policy=='hold':
                c['trim_out_ms']=c['source_duration_ms'];c['hold_last_ms']=duration-available;c['muted']=True
                warnings.append({'clip_id':cid,'code':'hold_last','message':'Последний кадр удерживается '+str(round((duration-available)/1000,2))+' с.'})
            elif policy=='speed' and available/duration>=.8:
                c['trim_out_ms']=c['source_duration_ms'];c['playback_rate']=available/duration;c['muted']=True
            else:
                conflict('short_video','Видео короче фразы. Выберите удержание последнего кадра или допустимую скорость 0.8–1×.',cid);continue
        c['voice_fit_auto']=True
        if spans[i]:c['voice_source_binding']=spans[i]
        changes.append({'clip_id':cid,'scene_id':c.get('scene_id'),'quote':(spans[i] or {}).get('quote',''),
            'old_start_ms':previous['start_ms'],'old_end_ms':previous.get('base_end_ms',previous['end_ms']),
            'new_start_ms':start,'new_end_ms':end,'hold_last_ms':c.get('hold_last_ms',0),'playback_rate':c.get('playback_rate',1)})
        if spans[i]:cues.append({'clip_id':cid,'start_ms':start,'end_ms':end,'quote':spans[i]['quote']})
    doc['timing_mode']='voice_fit'
    doc['voice_fit']={'version':VERSION,'audio_binding':copy.deepcopy(voice),
                      'story_sha256':hashlib.sha256(story.encode()).hexdigest(),'source_hash':source_signature(doc),'stale':False,'cues':cues}
    timeline=normalize(doc)
    if not conflicts and abs(timeline['duration_ms']-(offset+audio_end-audio_start))>.01:
        conflict('duration_mismatch','Расчёт не покрывает запись целиком.')
    return {'version':VERSION,'document':doc,'changes':changes,'conflicts':conflicts,'warnings':warnings,
            'can_apply':not conflicts,'timeline':timeline,'boundaries_ms':boundaries,
            'timing_accuracy_measured':False,'short_video_policy':policy}


def propose_duration(snap):
    """Keep source motion and narration speed; fill extra time with freeze tails.

    No inferred quotations, speech recognition or semantic alignment claims.
    """
    doc=copy.deepcopy(snap['document']);voice=snap['audio_binding']
    if not voice:raise DomainError('Добавьте и включите озвучку в монтаже.')
    target=voice['offset_ms']+voice['trim_out_ms']-voice['trim_in_ms']
    if not math.isfinite(target) or target<=0:raise DomainError('Пустой интервал озвучки.')
    active=[c for c in doc['clips'] if not c.get('excluded')]
    if not active:raise DomainError('Добавьте готовые клипы в монтаж.')
    old={c['edit_clip_id']:c for c in normalize(doc)['clips']}
    weights=[(c['trim_out_ms']-c['trim_in_ms'])/c.get('playback_rate',1)+c.get('hold_last_ms',0) for c in active]
    total=sum(weights)
    if not math.isfinite(total) or total<=0:raise DomainError('Некорректная длительность клипов.')
    boundaries=[0.0];used=0
    for weight in weights:
        used+=weight;boundaries.append(round(target*used/total,6))
    boundaries[-1]=target
    conflicts=[];changes=[]
    scenes={s['id']:s for s in snap['scenes']}
    for index,c in enumerate(active):
        cid=c['edit_clip_id'];previous=old[cid];start,end=boundaries[index:index+2];length=end-start
        if length<1000/24:
            conflicts.append({'code':'short_interval','clip_id':cid,'message':'Интервал короче одного видеокадра.'});continue
        if (c.get('timing_locked') or c.get('manual_timing')) and (
                abs(previous['start_ms']-start)>.01 or abs(previous.get('base_end_ms',previous['end_ms'])-end)>.01):
            conflicts.append({'code':'manual_boundary','clip_id':cid,'message':'Время клипа закреплено вручную. Снимите фиксацию для распределения по озвучке.'});continue
        scene=scenes.get(c.get('scene_id'))
        if scene and c.get('source_scene_revision')!=scene['current_revision']:
            conflicts.append({'code':'stale_scene','clip_id':cid,'message':'Сцена изменилась после добавления клипа; обновите источник.'});continue
        rate=c.get('playback_rate',1)
        moving=(c['trim_out_ms']-c['trim_in_ms'])/rate
        if length>=moving:
            c['hold_last_ms']=max(0,length-moving)
            if c['hold_last_ms']:c['muted']=True
        else:
            c['trim_out_ms']=c['trim_in_ms']+length*rate;c['hold_last_ms']=0
        c['voice_fit_auto']=True
        # A proportional clock is not evidence of words spoken in this clip.
        c.pop('voice_source_binding',None);c.pop('semantic_source_binding',None)
        changes.append({'clip_id':cid,'scene_id':c.get('scene_id'),'quote':'','label':'Клип '+str(index+1),
            'old_start_ms':previous['start_ms'],'old_end_ms':previous.get('base_end_ms',previous['end_ms']),
            'new_start_ms':start,'new_end_ms':end,'hold_last_ms':c['hold_last_ms'],
            'playback_rate':rate,'motion_ms':min(moving,length)})
    doc['timing_mode']='voice_fit'
    doc['voice_fit']={'version':VERSION,'method':'duration_freeze','phrase_alignment':False,
        'audio_binding':copy.deepcopy(voice),'story_sha256':hashlib.sha256(snap['story'].encode()).hexdigest(),
        'source_hash':source_signature(doc),'stale':False,'cues':[]}
    timeline=normalize(doc)
    if not conflicts and abs(timeline['duration_ms']-target)>.01:
        conflicts.append({'code':'duration_mismatch','message':'Расчёт не покрывает запись целиком.'})
    return {'version':VERSION,'method':'duration_freeze','document':doc,'changes':changes,
        'conflicts':conflicts,'warnings':[{'code':'duration_only','message':
            'Кадры распределены по общей длительности озвучки. Недостающее время заполнено стоп-кадрами; привязка по смыслу фраз не выполнялась.'}],
        'can_apply':not conflicts,'timeline':timeline,'boundaries_ms':boundaries,
        'timing_accuracy_measured':False,'short_video_policy':'freeze_trimmed_end'}


class MontageVoiceFit:
    def __init__(self,service):
        self.service=service;self.root=service.root/'montage-voice-fit'
        self.lock=threading.RLock();self.slot=threading.Semaphore(1);self.running={}
        from .release_paths import component_root
        self.base=component_root()

    def _path(self,pid,jid):
        self.service.editing.project(pid)
        if not isinstance(jid,str) or not re.fullmatch(r'[a-f0-9-]{32,36}',jid):raise DomainError('Вариант не найден.',404)
        # pid comes from a validated UUID project record, but verify containment
        # as well before creating the proposal directory.
        path=(self.root/pid/(jid+'.json')).resolve()
        if not path.is_relative_to(self.root.resolve()):raise DomainError('Недопустимый проект.')
        return path

    def _read(self,pid,jid):
        path=self._path(pid,jid)
        if not path.is_file():raise DomainError('Вариант подгонки не найден.',404)
        value=json.loads(path.read_text(encoding='utf-8'))
        if value['snapshot']['project_id']!=pid:raise DomainError('Вариант принадлежит другому проекту.')
        return value

    def _write(self,pid,jid,value):
        path=self._path(pid,jid);path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');os.replace(temporary,path)

    def _update(self,pid,jid,**fields):
        with self.lock:
            value=self._read(pid,jid);value.update(fields);self._write(pid,jid,value)

    def get(self,pid,jid):
        with self.lock:
            value=self._read(pid,jid)
            if value['status'] in ('queued','recognizing') and jid not in self.running:
                value.update(status='interrupted',stage='Прервано перезапуском. Создайте новый предпросмотр; распознанные части сохранены.')
                self._write(pid,jid,value)
        public={k:copy.deepcopy(v) for k,v in value.items() if k!='snapshot'}
        if public.get('proposal',{}).get('mapping'):public['proposal']['mapping'].pop('units',None)
        return public

    def start(self,pid,data):
        edit=self.service.editing.get(pid)
        if data.get('expected_revision')!=edit['revision']:raise DomainError('Сохраните текущий монтаж перед расчётом.',409)
        snap=snapshot(self.service,pid,edit)
        if not snap['audio_binding']:raise DomainError('Загрузите озвучку на дорожку монтажа.')
        policy=data.get('short_video_policy','hold')
        if policy not in ('hold','speed','error'):raise DomainError('Неизвестное правило короткого видео.')
        metadata=self.service.project(pid).get('analysis_metadata') or {}
        if isinstance(metadata,str):metadata=json.loads(metadata)
        mode=data.get('timing_mode') or ('duration_freeze' if metadata.get('prompt_import') else 'phrases')
        if mode not in ('phrases','duration_freeze','semantic'):raise DomainError('Неизвестный способ подгонки.')
        if mode=='semantic':
            from .montage_semantic import clip_texts
            clip_texts(snap)
        with self.lock:
            for jid,(other_pid,event) in self.running.items():
                if other_pid==pid:
                    old=self._read(pid,jid)
                    if signature(old['snapshot'])==signature(snap) and old['policy']==policy and old.get('timing_mode','phrases')==mode:return self.get(pid,jid)
                    raise DomainError('Для этого проекта уже рассчитывается подгонка. Дождитесь результата или отмените её.',409)
            jid=uid();event=threading.Event();self.running[jid]=(pid,event)
            self._write(pid,jid,{'id':jid,'snapshot':snap,'policy':policy,'timing_mode':mode,'status':'queued',
                                'stage':'Распределяю стоп-кадры по озвучке' if mode=='duration_freeze' else 'Ожидаю локальное распознавание речи','progress':0})
            threading.Thread(target=self._run,args=(pid,jid,event),daemon=True,name='montage-voice-fit').start()
        return self.get(pid,jid)

    def cancel(self,pid,jid):
        with self.lock:
            value=self._read(pid,jid)
            if jid in self.running:self.running[jid][1].set()
            if value['status'] in ('queued','recognizing'):
                value.update(status='cancelled',stage='Расчёт отменён; готовые клипы сохранены.');self._write(pid,jid,value)
        return self.get(pid,jid)

    def recognize(self,pid,jid,snap,event):
        from .voice_asr import repair_cache_matches
        asset=self.service.asset(snap['audio_binding']['asset_id']);story=snap['story']
        existing=self.service.root/'voice/asr'/(asset['sha256']+'.json')
        if existing.is_file():
            result=json.loads(existing.read_text(encoding='utf-8'))
            if repair_cache_matches(result,story):return result
        directory=self.root/'asr'/asset['sha256']/hashlib.sha256(story.encode()).hexdigest()
        directory.mkdir(parents=True,exist_ok=True)
        output=directory/'recognized.json';story_path=directory/'story.txt';progress=directory/'progress.json'
        if output.is_file():
            result=json.loads(output.read_text(encoding='utf-8'))
            if repair_cache_matches(result,story):return result
        python=self.base/'backends/voice-venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        model=self.base/'models/voice/turbo'
        if not python.is_file() or not all((model/p).is_file() for p in ('model.bin','config.json','tokenizer.json')):
            raise DomainError('Локальное распознавание речи не установлено. Запустите InstallVoice.ps1.',503)
        story_path.write_text(story,encoding='utf-8')
        with (directory/'recognition.log').open('w',encoding='utf-8') as log:
            process=subprocess.Popen([str(python),'-m','imagegen.voice_asr','--audio',str(self.service.root/asset['path']),
                '--model',str(model),'--output',str(output),'--progress',str(progress),'--story',str(story_path)],
                cwd=self.base,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                while process.poll() is None:
                    if event.wait(.5):raise InterruptedError()
                    if progress.is_file():
                        try:
                            p=json.loads(progress.read_text(encoding='utf-8'));fraction=min(1,p['processed_seconds']/max(1,p['total_seconds']))
                            self._update(pid,jid,progress=round(fraction*85),stage='Сопоставляю слова с озвучкой на ПК…')
                        except (ValueError,KeyError,OSError):pass
                if process.returncode:raise DomainError('Не удалось распознать озвучку. Подробности сохранены в журнале проекта.',503)
            finally:
                if process.poll() is None:process.terminate();process.wait(timeout=10)
        return json.loads(output.read_text(encoding='utf-8'))

    def _run(self,pid,jid,event):
        try:
            with self.slot:
                if event.is_set():raise InterruptedError()
                value=self._read(pid,jid);snap=value['snapshot']
                if value.get('timing_mode')=='duration_freeze':
                    proposal=propose_duration(snap)
                elif value.get('timing_mode')=='semantic':
                    proposal=self.semantic_proposal(pid,jid,snap,event)
                else:
                    self._update(pid,jid,status='recognizing',stage='Сопоставляю слова с озвучкой на ПК…')
                    asr=self.recognize(pid,jid,snap,event)
                    if event.is_set():raise InterruptedError()
                    from .voice_alignment import build_report
                    alignment=build_report({'project':{'id':pid,'story':snap['story']},'scenes':[]},asr)
                    proposal=propose(snap,alignment,value['policy'])
                with self.lock:
                    if event.is_set():raise InterruptedError()
                    self._update(pid,jid,status='ready',progress=100,stage='Предпросмотр готов. Монтаж ещё не изменён.',proposal=proposal)
        except InterruptedError:self._update(pid,jid,status='cancelled',stage='Расчёт отменён; монтаж сохранён.')
        except Exception as error:self._update(pid,jid,status='failed',stage='Расчёт остановлен',error=str(error))
        finally:
            with self.lock:self.running.pop(jid,None)

    def semantic_proposal(self,pid,jid,snap,event):
        from .montage_semantic import clip_texts,speech_units,match,propose
        self._update(pid,jid,status='recognizing',stage='Получаю временные метки речи на ПК…')
        asset=self.service.asset(snap['audio_binding']['asset_id'])
        if hashlib.sha256((self.service.root/asset['path']).read_bytes()).hexdigest()!=asset['sha256']:
            raise DomainError('Файл озвучки изменился на диске. Загрузите актуальную запись снова.',409)
        candidates=[self.service.root/'voice/asr'/(asset['sha256']+'.json')]
        candidates+=sorted((self.root/'asr'/asset['sha256']).glob('*/recognized.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        asr=None
        for path in candidates:
            if not path.is_file():continue
            try:
                value=json.loads(path.read_text(encoding='utf-8'))
                if (value.get('complete') and value.get('model')=='faster-whisper-large-v3-turbo' and
                    abs(value.get('audio_duration',0)*1000-next(a for a in snap['document']['audio_tracks'] if a['role']=='voice')['source_duration_ms'])<100):
                    asr=value;break
            except (ValueError,KeyError,OSError):continue
        if asr is None:
            # With no reference story the speech runtime performs acoustic
            # transcription only. Prompt text is never treated as narration.
            acoustic=copy.deepcopy(snap);acoustic['story']=''
            asr=self.recognize(pid,jid,acoustic,event)
        if event.is_set():raise InterruptedError()
        texts=clip_texts(snap);units=speech_units(asr,snap['audio_binding'],len(texts))
        all_texts=[x['embedding_text'] for x in texts]+[x['text'] for x in units]
        vectors=self.semantic_embeddings(pid,jid,all_texts,event)
        self._update(pid,jid,progress=95,stage='Сопоставляю сцены с фразами, сохраняя порядок…')
        mapping=match(snap,asr,vectors[:len(texts)],vectors[len(texts):],units)
        if event.is_set():raise InterruptedError()
        return propose(snap,mapping)

    def semantic_embeddings(self,pid,jid,texts,event):
        from .montage_semantic import MODEL_REVISION
        model=self.base/'models/semantic-mini'
        python=self.base/'backends/voice-venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        if not python.is_file() or not (model/'MODEL.json').is_file():
            raise DomainError('Модель смысловой подгонки не установлена. Запустите InstallSemanticFit.ps1.',503)
        spec=json.loads((model/'MODEL.json').read_text(encoding='utf-8'))
        if spec.get('revision')!=MODEL_REVISION or not (model/spec.get('file','missing')).is_file():
            raise DomainError('Файлы смысловой модели изменены. Запустите InstallSemanticFit.ps1.',503)
        for name,expected in spec.get('sha256',{}).items():
            path=(model/name).resolve()
            if not path.is_relative_to(model.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                raise DomainError('Проверка файлов смысловой модели не пройдена. Запустите InstallSemanticFit.ps1.',503)
        payload={'texts':texts,'model_revision':MODEL_REVISION}
        directory=self.root/'semantic-cache'/signature(payload);directory.mkdir(parents=True,exist_ok=True)
        source=directory/'input.json';output=directory/'embeddings.json';progress=directory/'progress.json'
        source.write_text(json.dumps(payload,ensure_ascii=False,sort_keys=True),encoding='utf-8')
        wanted=hashlib.sha256(source.read_bytes()).hexdigest()
        if output.is_file():
            cached=json.loads(output.read_text(encoding='utf-8'))
            if cached.get('input_sha256')==wanted:return cached['vectors']
        self._update(pid,jid,progress=40,stage='Сравниваю смысл промптов и речи локальной моделью…')
        with (directory/'embedding.log').open('w',encoding='utf-8') as log:
            process=subprocess.Popen([str(python),'-m','imagegen.semantic_runtime',
                '--input',str(source),'--model',str(model),'--output',str(output),'--progress',str(progress)],
                cwd=self.base,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                while process.poll() is None:
                    if event.wait(.4):raise InterruptedError()
                    if progress.is_file():
                        try:
                            p=json.loads(progress.read_text(encoding='utf-8'))
                            self._update(pid,jid,progress=40+round(50*p['processed']/max(1,p['total'])))
                        except (ValueError,KeyError,OSError):pass
                if process.returncode:raise DomainError('Ошибка локальной смысловой модели. Подробности в embedding.log проекта.',503)
            finally:
                if process.poll() is None:process.terminate();process.wait(timeout=10)
        cached=json.loads(output.read_text(encoding='utf-8'))
        if cached.get('input_sha256')!=wanted:raise DomainError('Результат относится к другим текстам.',409)
        return cached['vectors']

    def review(self,pid,jid,data):
        from .montage_semantic import propose
        with self.lock:
            value=self._read(pid,jid)
            if value['status']!='ready' or value.get('timing_mode')!='semantic':raise DomainError('Нет готовых смысловых привязок для проверки.',409)
            mapping=value['proposal']['mapping']
            if data.get('expected_review_revision')!=mapping.get('review_revision',0):raise DomainError('Привязки уже изменены. Обновите предпросмотр.',409)
            if signature(snapshot(self.service,pid,self.service.editing.get(pid)))!=signature(value['snapshot']):
                raise DomainError('Монтаж, промпты или озвучка изменились. Рассчитайте новый вариант.',409)
            decisions=data.get('decisions')
            if not isinstance(decisions,list) or len(decisions)>2000:raise DomainError('Некорректный список подтверждений.')
            proposal=propose(value['snapshot'],mapping,decisions)
            proposal['mapping']['review_revision']=mapping.get('review_revision',0)+1
            proposal['document']['voice_fit']['semantic_mapping']['review_revision']=proposal['mapping']['review_revision']
            value.update(proposal=proposal,stage='Проверка привязок сохранена. Монтаж ещё не изменён.')
            self._write(pid,jid,value)
        return self.get(pid,jid)

    def apply(self,pid,jid,data):
        with self.lock:
            value=self._read(pid,jid);current=self.service.editing.get(pid)
            if value['status']=='applied' and current['revision']==value.get('applied_revision'):return current
            if value['status']!='ready' or not value.get('proposal',{}).get('can_apply'):
                raise DomainError('Вариант содержит неразрешённые конфликты или ещё не готов.',409)
            if data.get('expected_revision')!=current['revision'] or signature(snapshot(self.service,pid,current))!=signature(value['snapshot']):
                raise DomainError('Проект, озвучка или монтаж изменились после предпросмотра. Рассчитайте новый вариант.',409)
            doc=copy.deepcopy(value['proposal']['document']);doc['voice_fit']['proposal_id']=jid
            def guard():
                if signature(snapshot(self.service,pid,self.service.editing.get(pid)))!=signature(value['snapshot']):
                    raise DomainError('Проект изменился после предпросмотра.',409)
            result=self.service.editing.save(pid,{'expected_revision':current['revision'],'document':doc},guard=guard)
            self._update(pid,jid,status='applied',stage='Подгонка применена',applied_revision=result['revision'],applied_hash=signature(result['document']))
            return result

    def rollback(self,pid,jid,data):
        with self.lock:
            value=self._read(pid,jid);current=self.service.editing.get(pid)
            if value['status']=='rolled_back' and current['revision']==value.get('rollback_revision'):return current
            if (value['status']!='applied' or data.get('expected_revision')!=current['revision']
                    or current['revision']!=value.get('applied_revision') or signature(current['document'])!=value.get('applied_hash')):
                raise DomainError('После подгонки монтаж изменён. Прямой откат перезапишет ручные правки; используйте историю изменений.',409)
            result=self.service.editing.save(pid,{'expected_revision':current['revision'],'document':value['snapshot']['document']})
            self._update(pid,jid,status='rolled_back',stage='Возвращён монтаж до подгонки',rollback_revision=result['revision'])
            return result


def subtitles(document):
    """Subtitle boundaries come from the exact normalized montage clock."""
    cues={c['clip_id']:c['quote'] for c in (document.get('voice_fit') or {}).get('cues',[])}
    def stamp(ms):
        ms=round(ms);s,ms=divmod(ms,1000);m,s=divmod(s,60);h,m=divmod(m,60)
        return f'{h:02}:{m:02}:{s:02},{ms:03}'
    lines=[]
    groups=[]
    for c in normalize(document)['clips']:
        origin=c.get('narration_origin_clip_id',c['edit_clip_id'])
        if origin not in cues:continue
        end=c.get('base_end_ms',c['end_ms'])
        if groups and groups[-1]['origin']==origin and abs(groups[-1]['end']-c['start_ms'])<.01:
            groups[-1]['end']=end
        else:groups.append({'origin':origin,'start':c['start_ms'],'end':end})
    for group in groups:
        lines.append(f"{len(lines)+1}\n{stamp(group['start'])} --> {stamp(group['end'])}\n{cues[group['origin']]}\n")
    return '\n'.join(lines)
