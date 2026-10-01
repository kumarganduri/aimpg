import importlib.util
import math
import sys
from pathlib import Path

from aimpg.attribution import Attribution
from aimpg.model import Request, Task, Usage

spec = importlib.util.spec_from_file_location(
    "attribution_eval", Path(__file__).resolve().parent.parent / "evals" / "attribution_eval.py"
)
ev = importlib.util.module_from_spec(spec)
sys.modules["attribution_eval"] = ev  # dataclasses look their module up here
spec.loader.exec_module(ev)


def req(session):
    return Request(id=session, session_id=session, model="m", ts=0.0, usage=Usage(output=1), cwd="/r")


def test_predicted_session_is_the_heaviest_weighted():
    t = Task(repo="/r", sha="c1", ts=0, subject="", attribution="fuzzy", status="kept")
    t.add(req("A"), 0.2)
    t.add(req("B"), 0.7)
    assert ev.predicted_sessions(Attribution(tasks=[t])) == {("/r", "c1"): ("B", "fuzzy")}


def test_scoring_precision_recall_and_false_claims():
    labels = [
        {"repo": "/r", "sha": "c1", "session": "A"},  # right
        {"repo": "/r", "sha": "c2", "session": "B"},  # wrong session
        {"repo": "/r", "sha": "c3", "session": None},  # hand commit the tool claimed
        {"repo": "/r", "sha": "c4", "session": "C"},  # missed
    ]
    predictions = {("/r", "c1"): ("A", "exact"), ("/r", "c2"): ("X", "exact"), ("/r", "c3"): ("A", "fuzzy")}
    result = ev.score(labels, predictions)
    assert result["precision"] == 1 / 3
    assert result["recall"] == 1 / 3
    assert result["false_claims_on_hand_commits"] == 1
    assert result["by_tier"]["exact"]["precision"] == 0.5
    assert result["by_tier"]["fuzzy"]["precision"] == 0.0


def test_no_predictions_gives_nan_precision():
    assert math.isnan(ev.score([{"repo": "/r", "sha": "c", "session": "A"}], {})["precision"])
