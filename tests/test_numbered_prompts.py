import json,subprocess,unittest
from imagegen.prompt_import import parse
from imagegen.domain import DomainError
from imagegen.generation_scene import BASE_BUDGET

class NumberedPromptTests(unittest.TestCase):
 def test_inline_wrapped_and_internal_brackets_preserved(self):
  parts=['[001. First [detail] and a line\nbreak.]','[002. Second scene.]']
  for mode in ('paragraphs','numbered'):
   self.assertEqual(parse('  '+' \n'.join(parts),mode),parts)
 def test_malformed_sequence_rejected_without_truncating(self):
  for s in ('[001. First.] [003. Third.]','[001. First.] trailing text','[001. First.] [002. Missing close','[001. ] [002. Second.]'):
   with self.subTest(s=s),self.assertRaises(DomainError):parse(s)
 def test_bracketed_reference_is_not_a_separator(self):
  self.assertEqual(parse('A scene with [12. detail] inside.'),['A scene with [12. detail] inside.'])
 def test_numbered_budget_is_still_enforced(self):
  with self.assertRaises(DomainError):parse('[001. '+('x'*BASE_BUDGET)+'] [002. Short.]')
 def test_browser_and_server_agree(self):
  cases=['[001. One.] [002. Two.]','[001. First [detail].]\n[002. Second.]','[001. First.] [003. Third.]','Normal\n\nOther']
  code="import fs from 'node:fs';import {parsePrompts} from './imagegen/static/prompt-import.mjs';console.log(JSON.stringify(JSON.parse(fs.readFileSync(0,'utf8')).map(s=>{try{return parsePrompts(s)}catch{return null}})));"
  result=subprocess.run(['node','--input-type=module','-e',code],input=json.dumps(cases),text=True,capture_output=True,check=True)
  expected=[]
  for s in cases:
   try:expected.append(parse(s))
   except DomainError:expected.append(None)
  self.assertEqual(json.loads(result.stdout),expected)
