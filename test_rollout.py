"""Checks of the rollout plan, the part that decides how many hosts a bad image can reach: python test_rollout.py"""
from collections import Counter

from rollout import waves


# The lab: 10 BMCs in 3 racks, 2441 marked canary: a canary, then waves of 3 (one per rack) and 6 (two per rack)
def test_lab():
    racks = ["r1", "r2", "r3", "r1", "r2", "r3", "r1", "r2", "r3", "r1"]
    servers = [{"host": f"h{n}", "rack": rack, **({"canary": True} if n == 1 else {})}
               for n, rack in enumerate(racks, 1)]
    plan = waves(servers, dict.fromkeys((s["host"] for s in servers), "- -"),
                 {"canary_per_model": 1, "waves": [33.0, 100.0], "max_per_rack": 2})
    assert [(name, [s["host"] for s in hosts]) for name, hosts in plan] == [
        ("canary", ["h1"]), ("wave 1", ["h2", "h3", "h4"]), ("wave 2", ["h5", "h6", "h7", "h8", "h9", "h10"])]


# A mixed fleet: the canary is the marked host of every model, no wave has two hosts of one rack, every host once
def test_fleet():
    servers = [{"host": f"n{i}", "rack": f"r{i % 100}", **({"canary": True} if i in (500, 501, 502) else {})}
               for i in range(1000)]
    models = {s["host"]: ("ASUS", "Dell", "HPE")[i % 3] for i, s in enumerate(servers)}
    plan = waves(servers, models, {"canary_per_model": 1, "waves": [5.0, 25.0, 100.0], "max_per_rack": 1})
    assert sorted(s["host"] for s in plan[0][1]) == ["n500", "n501", "n502"]
    assert sorted(models[s["host"]] for s in plan[0][1]) == ["ASUS", "Dell", "HPE"]
    assert all(max(Counter(s["rack"] for s in hosts).values()) == 1 for _, hosts in plan[1:])
    assert [len(hosts) for _, hosts in plan][:3] == [3, 50, 100]  # 5% of 997, then capped at 100 racks × 1
    assert sorted(s["host"] for _, hosts in plan for s in hosts) == sorted(s["host"] for s in servers)


if __name__ == "__main__":
    test_lab()
    test_fleet()
    print("ok")
