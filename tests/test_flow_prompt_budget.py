import json
import unittest

from imagegen.flow_prompt_budget import BASE_BUDGET, pack, units
from imagegen.visual_style import GRITTY_FLOW_STYLE


class FlowPromptBudgetTests(unittest.TestCase):
    def test_compacts_builtin_style_while_preserving_source_and_constraints(self):
        direction = "The rider raises her left hand beside the white dragon. " * 49
        contract = {
            "participants": ["rider", "dragon"],
            "direction": direction,
            "source_evidence": {"quote": "Только левую руку она подняла."},
            "forbidden": ["no human anatomy on the dragon"],
        }
        prompt = (
            GRITTY_FLOW_STYLE + ". " + direction +
            "Match character illustrative identity and location setting to attached reference images. "
            "Consistent digital art style, painterly rendering, original fictional characters, no celebrities, not a photograph."
            "\nFINAL RENDER CONTRACT (scene constraints; never image lettering):\n" + json.dumps(contract, ensure_ascii=False)
        )
        self.assertGreater(units(prompt), BASE_BUDGET)
        packed = pack(prompt)
        self.assertLessEqual(units(packed), BASE_BUDGET)
        self.assertIn(direction, packed)
        self.assertIn("Gritty Graphic Novel", packed)
        self.assertIn("no human anatomy on the dragon", packed)
        self.assertIn("Только левую руку она подняла.", packed)
        self.assertIn('"participants":["rider","dragon"]', packed)
        self.assertEqual(pack(packed), packed)

    def test_unique_oversized_source_is_still_over_budget(self):
        source = "Exact source. " * 400
        self.assertEqual(pack(source), source)
        self.assertGreater(units(pack(source)), BASE_BUDGET)
