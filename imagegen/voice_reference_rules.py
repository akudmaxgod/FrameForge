"""Explicit source rules for errors a semantic critic can still miss."""
import copy
import re


def guard_plan(plan,quote,previous_quote,anchors):
    result=copy.deepcopy(plan)
    city=next((a['canonical_name'] for a in anchors if a.get('anchor_id')=='anchor_city_master'),'')
    bath={a['time_layer']:a['canonical_name'] for a in anchors if a.get('anchor_id','').startswith('anchor_great_bath_')}
    subject=str(result['subject'])+' '+str(result['visual_direction'])
    if result.get('location_ref') in bath.values():
        if re.search(r'riverbed|river|русл|Сарасвати|Saraswati',subject,re.I) and not re.search(r'бассейн|Great\s*Bath|reservoir',subject,re.I):
            result['location_ref']=''
            result['reason']='River geography cannot use a reservoir architectural reference.'
    if re.search(r'бассейн[^.!?]*способен[^.!?]*сегодня',quote,re.I) and bath.get('modern_ruins'):
        result.update(subject='Современные руины Великого бассейна',subject_state='architecture',
            time_layer='modern_ruins',location_ref=bath['modern_ruins'],expected_people=0,roles=[],required_artifacts=[],
            visual_direction='Present-day Great Bath archaeological remains: exposed fired-brick reservoir walls, preserved steps and visible waterproofing surfaces. Show its physical construction in an outdoor documentary view. Do not claim it is presently filled with water; capacity to retain water is not evidence of its current water level.',
            forbidden=['intact ancient reconstruction','assumed present-day filled water level','modern swimming pool'],
            reason='The source discusses present capacity, not an observed filled pool. Show present remains without inventing a water level.')
    elif re.search(r'Велик\w*\s+бассейн|Great\s*Bath',str(result['subject']),re.I):
        layer='modern_ruins' if result['time_layer']=='excavation' else result['time_layer']
        if layer in bath:result['location_ref']=bath[layer]
    if result['required_artifacts'] and city and re.search(r'не воевали|мирн\w*\s+сосуществован|плотн\w*\s+городск\w*\s+сред',quote,re.I):
        result.update(subject='Мирная повседневная жизнь жителей города',subject_state='living',
            time_layer='historical_reconstruction',location_ref=city,expected_people=3,
            roles=['anonymous city resident']*3,required_artifacts=[],
            visual_direction='One inhabited ancient city street with dense source-described brick urban fabric. Three anonymous unarmed residents quietly walk and converse. Show peaceful ordinary daily life; no ruler, army, battle, ceremonial performance or museum artifact cutaway.',
            forbidden=['weapons','battle','ruler','unrelated museum artifacts'],
            reason='The narration concerns peaceful people and dense urban life; show that subject rather than an unrelated artifact.')
    if result['required_artifacts'] and re.search(r'аграрн\w*\s+технолог',quote,re.I) and re.search(r'жители ушли|переселен|унося с собой',previous_quote,re.I):
        result.update(subject='Переселение жителей и перенос земледельческих навыков',subject_state='living',
            time_layer='historical_reconstruction',location_ref='',expected_people=3,
            roles=['anonymous migrating resident']*3,required_artifacts=[],
            visual_direction='Three anonymous ancient migrants walk beside cultivated rural fields in the destination region described by the preceding narration, carrying simple agricultural tools and modest belongings. One coherent outdoor documentary reconstruction. No city reservoir, museum figurine or invented religious ceremony.',
            forbidden=['unrelated statue','city reservoir','invented ritual'],
            reason='The preceding quote describes migration; this continuation names the agricultural skills carried by the migrants.',
            contextual_source_quote=previous_quote)
    return result
