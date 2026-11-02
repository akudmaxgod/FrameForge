"""Narrow backstops for observed source contradictions, not a general fact oracle."""
import re
VERSION='frame-source-guards/1'

def fail(item,code,message,fields=('visual_direction',)):
    from .source_review_status import issue
    error=ValueError(message)
    error.source_issues=[issue(item,code,'semantic',message,evidence=[item['narration']],fields=list(fields))]
    raise error

def validate(value,item):
    from .narrative_fidelity import positive_description
    view=positive_description(value['visual_direction'])
    current=item['narration']['quote']
    sources=' '.join(q['quote'] for q in item.get('source_options',[]))
    if not sources:sources=current
    # Opposing motion phases attributed to one undivided actor/group are a
    # sequence, while one person ascending and another descending is a still.
    up=re.search(r'\b(?:climb\w*|ascend\w*)\b',view,re.I)
    down=re.search(r'\bdescend\w*\b',view,re.I)
    distribute=re.search(r'\b(?:another|others|other workers|one .{0,50}while|some .{0,50}while)\b',view,re.I)
    if up and down and not distribute:
        fail(item,'successive_motion_phases','Одна группа не может одновременно подниматься и спускаться в одном неподвижном кадре. Выберите один этап действия.')
    if (re.search(r'\b(?:Samu|Samo)\s+(?:mound|building|wall|structure|bridge)\b',view)
            and re.search(r'\bсам[уао]\s+(?:насыпь|здание|стену|сооружение|мост)\b',sources,re.I)):
        fail(item,'pronoun_as_object_name','Грамматическое местоимение стало вымышленным собственным именем объекта.',('subject','visual_direction'))
    if (re.search(r'у\s+(?:е[её]|его|насыпи|горы|холма)\s+подножия.{0,55}(?:город|поселение)',sources,re.I)
            and re.search(r'\b(?:mound|hill).{0,35}at\s+(?:the\s+)?(?:base|foot)\s+of\s+(?:a\s+|the\s+)?(?:city|town|settlement)\b',view,re.I)
            and not re.search(r'у\s+подножия\s+города',current,re.I)):
        fail(item,'reversed_spatial_relation','В источнике город у подножия насыпи; в изображении отношение обращено.')
    if (re.search(r'\bstone\s+(?:circle|posts?|pillars?)\b',view,re.I)
            and re.search(r'деревян\w*\s+(?:круг\w*|столб\w*)|wooden\s+(?:circle|posts?|pillars?)',sources,re.I)
            and not re.search(r'каменн\w*\s+(?:круг\w*|столб\w*)|stone\s+(?:circle|posts?|pillars?)',current,re.I)):
        fail(item,'changed_circle_material','Деревянный круг или столбы подменены каменными. Сохраните материал подтверждённого сооружения.')
    denied=(re.search(r'не\s+(?:доказ\w*|установ\w*)|доказать.{0,160}не\s+позвол|между.{0,180}доказан\w*.{0,180}оста[её]тся\s+расстояние',current,re.I))
    if (denied and value.get('source_status') not in ('reported','hypothesis','conditional')
            and re.search(r'flood[- ](?:driven|caused)|(?:flood\w*.{0,35}(?:destroy|collapse)|(?:destroy|collapse).{0,35}flood)',view,re.I)):
        fail(item,'denied_disaster_causality','Отсутствие доказанной причины превращено в положительное описание разрушения от наводнения.',('action','source_status','visual_direction'))
    if (re.search(r'осторожн\w*.{0,100}истори\w*.{0,100}непригодн\w*',current,re.I)
            and re.search(r'\buninhabitable\s+(?:land|soil)|\bland\s+(?:is|was)\s+uninhabitable',view,re.I)):
        fail(item,'caution_as_positive_claim','Оговорка о недоказанной непригодности земли превращена в утверждение её непригодности.',('source_status','visual_direction'))
    if (re.search(r'изотоп\w*|isotop\w*',sources,re.I) and re.search(r'почв\w*|soil',sources,re.I)
            and re.search(r'не\s+обнаруж|не\s+наш',current,re.I)
            and re.search(r'unchanged\s+(?:plant\s+cover|vegetation)|observe.{0,55}(?:ancient|unchanged)\s+(?:plant|vegetation)',view,re.I)):
        fail(item,'proxy_as_direct_observation','Вывод по древним почвам подменён непосредственным наблюдением живой древней растительности.')
