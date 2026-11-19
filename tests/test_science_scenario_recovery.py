import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from imagegen.science_atlas_render import compile_commands
from imagegen.science_planner import Plan, Review, source_groups, stage
from imagegen.script_analysis.cache import DiskCache


class FixturePlanner:
    """Stand-in for model output. Exercises batching, validation and compilation."""

    def __init__(self):
        self.generated=[]

    def generate_plan(self, payload, schema):
        assets=[]
        shots=[]
        for frame in payload['frames']:
            number=frame['n']
            quote=frame['narration'][:1200]
            key=f'fixture_{number}'
            assets.append({'key':key,'identity':'Часы','view':'вид сбоку','state':'наблюдение',
                           'subject':'Крупный план круглых часов с читаемой формой циферблата'})
            shots.append({'n':number,'layout':'hero_caption','function':'observation','representation':'literal',
                          'focus':'Поясняющая иллюстрация к фрагменту','support_quote':quote,'headline':'Наблюдение',
                          'asset_keys':[key],'labels':['Тень от гномона','Полуденная линия'],'frame_density':'filled'})
            self.generated.append(number)
        return schema.model_validate({'assets':assets,'shots':shots})

    def ask_json(self,messages,schema,*args,**kwargs):
        return Review(issues=[])


class ScienceScenarioRecoveryTests(unittest.TestCase):
    def test_long_scenario_compiles_all_batches_and_second_run_reuses_cache(self):
        example=os.environ.get('FRAMEFORGE_TEST_SCIENCE_SCENARIO')
        story=Path(example).read_text(encoding='utf-8-sig') if example else '\n\n'.join(
            f'Наблюдение {i}. Сравним положение тени на круглом циферблате утром и ближе к полудню.' for i in range(40))
        frames=source_groups(story)
        with TemporaryDirectory() as tmp,patch('imagegen.science_planner.JsonClient',side_effect=AssertionError('Paid client forbidden')):
            local=FixturePlanner()
            cache=DiskCache(Path(tmp))
            initial={'characters':[],'scenes':[]}
            result=stage(initial,story,cache=cache,local_review=local)
            self.assertEqual(local.generated,list(range(1,len(frames)+1)))
            for scene,(start,end) in zip(result['scenes'],frames,strict=True):
                self.assertEqual(scene['source_quote'],story[start:end])
                plan=scene['science_atlas']
                rendered=compile_commands({**plan,'illustrations':[{'key':key,'asset_id':None} for key in plan['asset_keys']]})
                self.assertTrue(rendered['commands'])
            local.generated.clear()
            cached=stage(initial,story,cache=cache,local_review=local)
            self.assertEqual(result['scenes'],cached['scenes'])
            self.assertEqual(local.generated,[])
