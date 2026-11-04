import json
import tempfile
import unittest
from pathlib import Path

from bridgelab.agent import run_role
from bridgelab.clients import FakeMuseClient
from bridgelab.ledger import Ledger
from bridgelab.muse_runtime import validate_selection


def response(model, content):
    return {"model": model, "choices": [{"message": {"content": content}}],
            "usage": {"total_tokens": 1}}


class AgentContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(str(Path(self.tmp.name) / "run.jsonl"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_wrong_model_is_rejected(self):
        client = FakeMuseClient([response("other-model", "{}")])
        result = run_role(client, self.ledger, "problem_structurer", "question", max_retries=0)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "model_id_mismatch")

    def test_missing_model_is_rejected(self):
        client = FakeMuseClient([{"choices": [{"message": {"content": "{}"}}]}])
        result = run_role(client, self.ledger, "problem_structurer", "question", max_retries=0)
        self.assertEqual(result["error"], "model_id_missing")

    def test_invalid_json_retries_and_then_fails(self):
        client = FakeMuseClient([
            response("muse-spark-1.3", "not json"),
            response("muse-spark-1.3", "still not json"),
            response("muse-spark-1.3", "nope"),
        ])
        result = run_role(client, self.ledger, "problem_structurer", "question")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempts"], 3)

    def test_selection_rejects_unknown_ids_and_truncates_budget(self):
        invalid = validate_selection(["x"], ["a", "b"], 1)
        self.assertEqual(invalid["status"], "invalid")
        limited = validate_selection(["a", "b"], ["a", "b"], 1)
        self.assertEqual(limited["ids"], ["a"])
        self.assertIn("truncated_to_budget", limited["flags"])


if __name__ == "__main__":
    unittest.main()
