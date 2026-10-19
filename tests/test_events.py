import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events


class TestEvents(unittest.TestCase):
    def test_instrument_traces_calls(self):
        tmp_path = Path(tempfile.mkdtemp())
        events.EVENTS_DIR = tmp_path
        tg = types.SimpleNamespace(
            send_job_for_approval=lambda job, nonce="": 1,
            wait_for_decision=lambda job_id, nonce="", timeout=0: "approve")

        async def apply_to_job(job_id, pdf, label, mode="dry"):
            return {"status": "applied"}

        def tailor_with_claude(job, keywords=None):
            tg.send_job_for_approval(job)  # nested: must not emit its own line
            return {"ok": True}

        def boom(job, d):
            raise ValueError("no")

        pipe = types.SimpleNamespace(
            log=lambda m: None, tg=tg, ra=types.SimpleNamespace(extract_keywords=boom),
            tailor=types.SimpleNamespace(tailor_with_claude=tailor_with_claude),
            auto_apply=types.SimpleNamespace(apply_to_job=apply_to_job))
        real_sup = sys.modules.get("supervisor")
        sys.modules["supervisor"] = types.SimpleNamespace()
        self.addCleanup(lambda: sys.modules.__setitem__("supervisor", real_sup)
                        if real_sup else sys.modules.pop("supervisor", None))
        events.instrument(pipe)
        events.instrument(pipe)  # idempotent

        job = {"job_id": "42", "title": "ML Co-op"}
        pipe.tg.send_job_for_approval(job)
        assert pipe.tg.wait_for_decision("42") == "approve"
        pipe.tailor.tailor_with_claude(job)
        assert asyncio.run(pipe.auto_apply.apply_to_job("42", "x.pdf", "l"))["status"] == "applied"
        try:
            pipe.ra.extract_keywords(job, None)
        except ValueError:
            pass
        pipe.log("hello")

        evs = [json.loads(l) for l in (tmp_path / f"{events.RUN}.jsonl").read_text().splitlines()]
        got = [(e["agent"], e["ev"]) for e in evs]
        assert got == [("pipeline", "start"), ("telegram", "msg"),
                       ("telegram", "start"), ("telegram", "end"),
                       ("tailor", "start"), ("tailor", "end"),
                       ("apply", "start"), ("apply", "end"),
                       ("keywords", "start"), ("keywords", "error"),
                       ("pipeline", "log")], got
        assert evs[3]["text"] == "approve" and evs[3]["title"] == "ML Co-op"
        assert evs[7]["text"] == "status=applied" and evs[7]["job"] == "42"


if __name__ == "__main__":
    unittest.main()
