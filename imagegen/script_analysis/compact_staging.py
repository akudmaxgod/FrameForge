"""Compact pre-generation staging, cached independently for each frame batch."""
import copy
from dataclasses import asdict
import hashlib
import json

from pydantic import Field

from .cache import DiskCache, atomic_json
from .errors import AnalysisError
from .schemas import StrictModel
from .stats import Stats


def signature(analysis, story):
    return hashlib.sha256((PROMPT + json.dumps({'story': story, 'characters': analysis['characters'],
        'frames': [(s.get('source_start'), s.get('source_end'), s.get('source_quote'))
                   for s in analysis['scenes']]}, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()

PROMPT = """Ты режиссёр смысловой раскадровки. Верни JSON {"episodes":[{"place":"место",
"time":"момент хронологии","state":"возраст, ранения, плен; состояния только видимых героев",
"kind":"event|report|memory|condition|comment",
"shots":[{"n":1,"cast":["ch_1"],"roles":[],"action":"короткое конкретное видимое действие"}]}]}.
Объединяй последовательные планы одного события в эпизод. Общие place,time,state пиши один раз.
Каждый входной n должен встретиться ровно один раз и по порядку. cast содержит только ID видимых
героев реестра. roles — конкретные безымянные участники, когда они нужны источнику.
Сначала восстанови причинные связи всей истории. Сохраняй участников и место при разрыве предложения
между кадрами. Адресат разговора и обсуждаемый человек различаются. Новости показывай как передачу
информации, а не встречу с удалённым героем. Условие не становится состоявшимся фактом.
Мёртвый герой может появиться только в обозначенном воспоминании. Для воспоминаний сохрани возраст.
Комментарий иллюстрируй установленным событием, реакцией или предметной деталью; не выдумывай совещания.
Не добавляй событий и знаний, которых нет у героев на этом этапе. Текст narration описывает историю,
а action — только то, что должно быть видно. Сохрани смысл каждой реплики в контексте соседних.
Пиши кратко. Внешность, стиль, камеру, технические ограничения добавит приложение."""


class Shot(StrictModel):
    n: int
    cast: list[str]
    roles: list[str]
    action: str = Field(min_length=1, max_length=1200)


class Episode(StrictModel):
    place: str = Field(min_length=1, max_length=400)
    time: str = Field(min_length=1, max_length=400)
    state: str = Field(max_length=800)
    kind: str
    shots: list[Shot]


class Staging(StrictModel):
    episodes: list[Episode]


def validate(plan, frames, characters):
    shots = [shot for episode in plan.episodes for shot in episode.shots]
    if [shot.n for shot in shots] != [frame['n'] for frame in frames]:
        raise AnalysisError('Смысловая постановка пропустила или переставила кадры. Генерация не запущена.')
    for episode in plan.episodes:
        if not episode.shots or episode.kind not in ('event', 'report', 'memory', 'condition', 'comment'):
            raise AnalysisError('Некорректный эпизод смысловой постановки.')
        for shot in episode.shots:
            if len(set(shot.cast)) != len(shot.cast) or not set(shot.cast) <= characters.keys():
                raise AnalysisError('Смысловая постановка ссылается на неизвестного персонажа.')
            if any(not role.strip() for role in shot.roles):
                raise AnalysisError('Не определён участник смысловой постановки.')
    return plan



def reconcile(plans, frames, characters):
    """Accept only uniquely mapped, valid shots; preserve each episode's context."""
    expected = {frame['n'] for frame in frames}
    candidates = {}
    for plan in plans:
        for episode in plan.episodes:
            for shot in episode.shots:
                if shot.n in expected:
                    candidates.setdefault(shot.n, []).append((episode, shot))
    accepted = {}
    for number, entries in candidates.items():
        if len(entries) != 1:
            continue
        episode, shot = entries[0]
        try:
            validate(Staging(episodes=[episode.model_copy(update={'shots': [shot]})]),
                     [{'n': number}], characters)
        except AnalysisError:
            continue
        accepted[number] = (episode, shot)
    return accepted


def ordered_plan(accepted, frames):
    episodes = []
    last = None
    for frame in frames:
        episode, shot = accepted[frame['n']]
        context = episode.model_dump(exclude={'shots'})
        if context != last:
            episodes.append(episode.model_copy(update={'shots': []}))
            last = context
        episodes[-1].shots.append(shot)
    return Staging(episodes=episodes)


def stage(analysis, story, *, api_key=None, client=None, cache=None, progress=None):
    source_key = signature(analysis, story)
    previous = analysis.get('diagnostics', {}).get('compact_staging', {})
    if previous.get('complete') and previous.get('source_key') == source_key:
        return analysis
    result = copy.deepcopy(analysis)
    characters = {c.get('cast_id') or c['id']: c for c in result['characters']}
    registry = [{'id': key, 'name': c['name'], 'aliases': c.get('aliases', []),
                 'role': c.get('role', '')} for key, c in characters.items()]
    frames = [{'n': i + 1, 'narration': s['source_quote']} for i, s in enumerate(result['scenes'])]
    cache = cache or DiskCache()
    stats = Stats()
    owned = client is None
    hits = 0
    carry = []
    try:
        for start in range(0, len(frames), 40):
            batch = frames[start:start + 40]
            payload = {'story': story, 'registry': registry, 'previous': carry,
                       'frames': batch, 'following': frames[start + 40:start + 42]}
            key = hashlib.sha256((PROMPT + json.dumps(payload, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()
            path = cache.directory / ('staging-' + key + '.json')
            with cache.lock('staging-' + key):
                plan = None
                cached_path=cache.read_path(path)
                if cached_path.is_file():
                    try:
                        plan = validate(Staging.model_validate_json(cached_path.read_text(encoding='utf-8')), batch, characters)
                        hits += 1
                    except (ValueError, AnalysisError):
                        plan = None
                if plan is None:
                    # The two primary passes already contain a complete breakdown.
                    # Optional narrative enrichment must not silently add a billable pass.
                    if owned:
                        return result
                    if progress:
                        progress(f'Постановка истории: кадры {start + 1}–{start + len(batch)}', 70)
                    draft_path = path.with_suffix('.draft.json')
                    if draft_path.is_file():
                        try:
                            plan = Staging.model_validate_json(draft_path.read_text(encoding='utf-8'))
                        except ValueError:
                            plan = None
                    if plan is None:
                        plan = client.ask_json([{'role': 'system', 'content': PROMPT},
                            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}],
                            Staging, stats, phase='C', script_hash=key)
                        atomic_json(draft_path, plan.model_dump())
                    accepted = reconcile([plan], batch, characters)
                    for attempt in range(2):
                        missing = [frame for frame in batch if frame['n'] not in accepted]
                        if not missing:
                            break
                        if progress:
                            progress(f"Уточняю постановку {len(missing)} кадров", 70)
                        repair_payload = {**payload, 'frames': missing,
                            'required_numbers': [frame['n'] for frame in missing],
                            'instruction': 'Верни отдельный shots для КАЖДОГО required_numbers. '
                                'Не объединяй номера, не меняй нумерацию, не возвращай кадры из previous/following. '
                                'Общие сведения можно объединить в episodes.'}
                        repaired = client.ask_json([{'role': 'system', 'content': PROMPT},
                            {'role': 'user', 'content': json.dumps(repair_payload, ensure_ascii=False, separators=(',', ':'))}],
                            Staging, stats, phase='C', script_hash=key)
                        accepted.update(reconcile([repaired], missing, characters))
                        partial_frames = [frame for frame in batch if frame['n'] in accepted]
                        atomic_json(draft_path, ordered_plan(accepted, partial_frames).model_dump())
                    missing = [frame['n'] for frame in batch if frame['n'] not in accepted]
                    if missing:
                        raise AnalysisError(f'Не удалось уточнить постановку кадров {missing}. '
                            'Готовая часть сохранена; повтор продолжит с оставшихся кадров.')
                    plan = validate(ordered_plan(accepted, batch), batch, characters)
                    atomic_json(path, plan.model_dump())
            for episode in plan.episodes:
                for shot in episode.shots:
                    scene = result['scenes'][shot.n - 1]
                    names = [characters[cid]['name'] for cid in shot.cast]
                    direction = '. '.join(filter(None, [episode.place, episode.time, episode.state, shot.action]))
                    scene.update(mentioned_characters=[], offscreen_characters=[], voice_only_characters=[],
                        beat=direction, composition=direction, audit_visual_direction=direction,
                        audit_source_quote=scene['source_quote'], characters=names, character_ids=shot.cast,
                        cast=[{'character_id': cid, 'name': characters[cid]['name']} for cid in shot.cast],
                        episodic_people=shot.roles, background_roles=[], depicted_people=[],
                        visible_people=len(names) + len(shot.roles), raw_visible_people=len(names) + len(shot.roles),
                        population_policy='scene_participants' if names or shot.roles else 'object_only',
                        wardrobe={name: scene.get('wardrobe', {}).get(name, 'default') for name in names},
                        location=episode.place, location_ref=episode.place, location_id=None,
                        time=episode.time, compact_staging={'version': key, 'kind': episode.kind,
                            'state': episode.state, 'action': shot.action})
            last = plan.episodes[-1]
            carry = [{**last.model_dump(exclude={'shots'}), 'shots': [last.shots[-1].model_dump()]}]
        usage = result.setdefault('deepseek_usage', {})
        for name, amount in (('tokens_in', stats.tokens_in), ('tokens_out', stats.tokens_out),
                             ('cost_usd', stats.cost_usd), ('cache_hit_tokens', stats.cache_hit_tokens),
                             ('cache_miss_tokens', stats.cache_miss_tokens)):
            usage[name] = usage.get(name, 0) + amount
        usage['compact_staging_cost_usd'] = stats.cost_usd
        result.setdefault('diagnostics', {})['compact_staging'] = {
            'complete': True, 'source_key': source_key, 'usage': asdict(stats), 'cache_batches': hits, 'frames': len(frames)}
        return result
    finally:
        if owned and client is not None:
            client.close()
