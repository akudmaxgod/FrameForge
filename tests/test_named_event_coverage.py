import unittest,copy
from imagegen.named_event_coverage import audit,repair

def char(name,cid):return {'name':name,'cast_id':cid,'aliases':[name],'description':'Персонаж'}
def scene(name,cid,quote,start=0):return {'beat':quote,'characters':[name],'character_ids':[cid],'source_start':start,'source_end':start+len(quote),'source_quote':quote,'location':'Современная комната'}

class EventCoverageTests(unittest.TestCase):
 def test_missing_historical_actor_is_recovered_without_inheriting_modern_scene(self):
  story='Анна вошла в комнату.\n\nВ девятнадцатом веке Борис участвовал в экспедиции.'
  a={'characters':[char('Анна','ch_1'),char('Борис','ch_2')],'scenes':[scene('Анна','ch_1',story)]}
  out=repair(a,story);self.assertEqual(len(out['scenes']),2)
  added=next(s for s in out['scenes'] if 'event_coverage_recovery' in s)
  self.assertEqual(added['characters'],['Борис']);self.assertEqual(added['location'],'')
  self.assertEqual(story[added['source_start']:added['source_end']],added['source_quote'])
  self.assertNotIn('Анна',added['beat']);self.assertEqual(len(repair(out,story)['scenes']),2)
  from imagegen.source_contract import coverage
  self.assertTrue(coverage(story,out['scenes'])['ok'])
  self.assertEqual(added['narration_policy'],'reuse_existing_audio')
  self.assertEqual(len(a['scenes']),1)
 def test_fantasy_and_travel_are_not_tied_to_medical_names(self):
  for story in ['Вечером Лея вошла в башню.','На рассвете Марк отправился в путешествие.']:
   name='Лея' if 'Лея' in story else 'Марк'
   out=repair({'characters':[char(name,'ch_9')],'scenes':[]},story)
   self.assertEqual(len(out['scenes']),1);self.assertEqual(out['scenes'][0]['source_quote'],story)
 def test_mentions_statues_negation_hypotheses_and_attribution_do_not_add_people(self):
  for story in ['По мнению Бориса, поездка была опасной.','Статуя Борис стоит в зале.','Борис не участвовал в поездке.','Возможно Борис участвовал в поездке.','Борис писал о поездке.','В конце главы упоминается Борис.']:
   out=repair({'characters':[char('Борис','ch_1')],'scenes':[]},story)
   self.assertEqual(out['scenes'],[],story)
 def test_visible_actor_and_object_of_another_action_are_not_duplicated(self):
  story='Анна осмотрела Бориса.'
  a={'characters':[char('Анна','ch_1'),char('Борис','ch_2')],'scenes':[scene('Анна','ch_1',story)]}
  self.assertEqual(repair(a,story)['scenes'],a['scenes'])
 def test_same_name_is_not_guessed_between_two_registry_identities(self):
  a=char('Борис','ch_1');b=char('Борис','ch_2')
  self.assertFalse(audit('Борис вошёл в комнату.',[a,b],[])['events'])
 def test_limit_is_not_silently_exceeded(self):
  a={'characters':[char('Борис','ch_2')],'scenes':[scene('Анна','ch_1','Анна вошла.')]}
  with self.assertRaises(ValueError):repair(a,'Борис вошёл в комнату.',max_scenes=1)
 def test_report_never_claims_full_semantic_completeness(self):
  self.assertFalse(audit('Объяснение без названных людей.',[],[])['semantic_completeness_measured'])

if __name__=='__main__':unittest.main()
