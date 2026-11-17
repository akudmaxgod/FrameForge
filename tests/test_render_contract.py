import unittest,copy,json
from imagegen.source_contract import render_contract

class RenderContractTests(unittest.TestCase):
 def document(self,quote):
  return {'hash':'audit-hash','source':{'start':0,'end':len(quote),'quote':quote},'visual_source':{'start':0,'end':len(quote),'quote':quote},
   'context':quote,'direction':'Один пациент держится за живот.','participants':['patient-id'],
   'subject_state':'Опухшая левая щека','period':'XIX век','time_layer':'historical',
   'required_artifacts':['стол'],'forbidden':['не добавлять второго пациента'],
   'review_plan':{'must_show':['левая щека'],'forbidden':['не переносить в современность'],'status':'pass'}}
 def test_large_audit_quotes_do_not_repeat_in_the_image_prompt(self):
  document=self.document('Полный исходный сценарий. '*800);before=copy.deepcopy(document)
  rendered=render_contract(document);wire=json.dumps(rendered,ensure_ascii=False)
  self.assertLess(len(wire),2000);self.assertNotIn(document['context'],wire)
  self.assertEqual(rendered['audit_contract_hash'],document['hash'])
  self.assertEqual(rendered['source_evidence']['end'],len(document['context']))
  self.assertTrue(rendered['source_evidence']['full_quote_retained']);self.assertEqual(document,before)
 def test_all_explicit_visual_constraints_remain_complete(self):
  d=self.document('Пациент сидит.');r=render_contract(d)
  for key in ('direction','participants','subject_state','period','time_layer','required_artifacts','forbidden'):
   self.assertEqual(r[key],d[key])
  self.assertEqual(r['review_constraints']['must_show'],['левая щека'])
  self.assertEqual(r['review_constraints']['forbidden'],['не переносить в современность'])
 def test_short_exact_evidence_is_included_once(self):
  quote='Пациент сидит.';r=render_contract(self.document(quote))
  self.assertEqual(r['source_evidence']['quote'],quote)
  self.assertEqual(json.dumps(r,ensure_ascii=False).count(quote),1)
 def test_long_visual_conditions_are_never_silently_cut(self):
  d=self.document('Короткая цитата.');d['forbidden']=['X'*13000+' END CONDITION']
  self.assertEqual(render_contract(d)['forbidden'],d['forbidden'])
 def test_manual_direction_and_editorial_decisions_are_preserved(self):
  d=self.document('Цитата.');d.update(manual_decision={'applied':True,'direction':'Только руки.'},editorial_decision={'omit_claims':['Непроверенное утверждение']})
  r=render_contract(d);self.assertEqual(r['manual_decision'],d['manual_decision']);self.assertEqual(r['editorial_decision'],d['editorial_decision'])

if __name__=='__main__':unittest.main()
