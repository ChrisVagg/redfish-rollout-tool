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


# Push or pull, against what the BMC allows: a target or a URL scheme outside AllowableValues means no, before any drain
def test_ways_in():
    import poller
    real = poller.update_targets_of  # ways_in looks it up in poller
    poller.update_targets_of = lambda found, component: ["/redfish/v1/Managers/bmc"]
    found = {"/redfish/v1/UpdateService": {
        "@odata.type": "#UpdateService.v1_11_0.UpdateService", "MultipartHttpPushUri": "/redfish/v1/UpdateService/up",
        "Actions": {"#UpdateService.SimpleUpdate": {
            "target": "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
            "TransferProtocol@Redfish.AllowableValues": ["HTTPS"],
            "Targets@Redfish.AllowableValues": ["/redfish/v1/Managers/bmc"]}}}}
    push, pull = poller.ways_in(found, "Manager (BMC)", "https://mirror/bmc.tar")
    assert push == "yes · /redfish/v1/UpdateService/up" and pull.startswith("yes · HTTPS")
    assert poller.ways_in(found, "Manager (BMC)", "http://mirror/bmc.tar")[1].startswith("no: HTTP not in")
    poller.update_targets_of = lambda found, component: ["/redfish/v1/Systems/system/Bios"]
    assert "not in Targets@AllowableValues" in poller.ways_in(found, "System BIOS")[1]
    poller.update_targets_of = real


# Metrics are built mid-run, when a host can have a failed update task and no end yet: that must not raise
def test_metrics_mid_run():
    import rollout
    at = lambda s: f"2026-10-02T18:{s}+00:00"
    events = [
        {"at": at("00:00"), "host": "-", "step": "run", "state": "start", "waves": {"wave 1": ["h1"]}, "halt_at": 0.1},
        {"at": at("00:01"), "host": "h1", "step": "pre-flight", "state": "go", "running": "1.0", "target": "2.0"},
        {"at": at("00:02"), "host": "h1", "step": "update", "state": "start", "method": "push"},
        {"at": at("00:09"), "host": "h1", "step": "update", "state": "Exception", "task": {
            "uri": "/redfish/v1/TaskService/Tasks/0", "state": "Exception", "messages": [
                {"severity": "Critical", "message_id": "TaskEvent.1.0.TaskAborted", "message": "aborted"}]}}]
    doc = rollout.pipeline_report(None, events, "run", [])
    assert doc["result"] == "running" and doc["waves"][0]["hosts"][0]["state"] == "running"
    text = rollout.metrics(doc)
    assert 'rollout_hosts{wave="wave 1",state="running"} 1' in text and "rollout_running 1" in text


# The exporter's samples are one series each: two sensors of one name (iLO 4's power supplies) stay two series.
# Thresholds take DMTF's Sensor names and their reading's unit, a Fan's too (its schema gives them none)
def test_exposition():
    import poller
    poller.schema_file = lambda name: {}  # offline: the unit comes from ReadingUnits
    # lambda n: the n-th sensor, every one named PSU
    sensor = lambda n: {"@odata.type": "#Sensor.v1_2_0.Sensor", "Name": "PSU", "Reading": 30 + n, "ReadingUnits": "Cel",
                        "Thresholds": {"UpperCritical": {"Reading": 90}}, "Status": {"Health": "Warning"}}
    fan = {"Name": "Fan1", "Reading": 5000, "ReadingUnits": "RPM", "LowerThresholdNonCritical": 600,
           "UpperThresholdCritical": 0}  # an upper 0: no threshold set (iLO 4)
    found = {poller.ROOT: {}, **{f"/redfish/v1/Chassis/1/Sensors/{n}": sensor(n) for n in (1, 2)},
             "/redfish/v1/Chassis/1/Thermal": {"@odata.type": "#Thermal.v1_7_0.Thermal", "Fans": [fan]}}
    out = poller.exposition({"host": "h"}, found, 1.0)
    series = [line.rsplit(" ", 1)[0] for line in out]
    assert len(series) == len(set(series)) and sum(s.startswith("redfish_reading{") for s in series) == 3, series
    thresholds = sorted(line for line in out if line.startswith("redfish_reading_threshold"))
    assert len(thresholds) == 3 and all('threshold="UpperCritical"} 90' in t for t in thresholds[1:]), thresholds
    assert 'sensor="Fan1",property="Reading",unit="RPM",threshold="LowerCaution"} 600' in thresholds[0], thresholds
    assert sum(line.startswith("redfish_health{") and 'health="Warning"' in line for line in out) == 2
    crossed = sorted(line.rsplit(" ", 1)[1] for line in out if line.startswith("redfish_reading_crossed"))
    assert crossed == ["0", "0", "0"], crossed  # the sensors under their 90, the fan over its 600
    fan["Reading"] = 500  # under its lower caution
    assert any(line.startswith("redfish_reading_crossed{") and 'sensor="Fan1"' in line and line.endswith(" 1")
               for line in poller.exposition({"host": "h"}, found, 1.0))


if __name__ == "__main__":
    test_lab()
    test_fleet()
    test_ways_in()
    test_metrics_mid_run()
    test_exposition()
    print("ok")
