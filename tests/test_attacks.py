"""The "try to cheat" test page's attacks, run against the real gateway (in-process): every one must be refused."""
from app import attacks, keys
from tests.conftest import NOW
from tests.test_gateway import MONDAY


def test_every_cheating_attempt_is_refused(world):
    old = world.mandates["hotel"]
    current = keys.amend_mandate("hotel", old, {"approver_name": "Mikko Laine"}, "mikko.laine", "test")
    world.client.publish_mandate(current.model_dump(mode="json"))
    results = attacks.run(world.client, current, MONDAY, older=old, now=NOW)
    assert [(a.title, a.outcome) for a in results if not a.safe] == []
    assert len(results) == 7 and all(a.reason and a.raw for a in results)
