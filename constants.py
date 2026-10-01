"""Constants of the Redfish poller: what to crawl, how to read it, and table styling."""
import os
import re
from pathlib import Path

import requests

ROOT = "/redfish/v1"
# Value of every key ending in @odata.id, @odata.nextLink (paged collections) or @Redfish.ActionInfo.
LINKS = re.compile(r'"[^"]*@(?:odata\.id|odata\.nextLink|Redfish\.ActionInfo)": "([^"]+)"')
TYPE = re.compile(r"#?([^.]*)(?:\.(v\d+_\d+_\d+))?")  # '#Chassis.v1_14_0.Chassis' -> Chassis, v1_14_0
# Not discovered: log services and entries, sessions, accounts, certificates, schema files, registries,
# event subscriptions, and per-core/per-thread sub-processors (the CPU has TotalCores and TotalThreads). They are
# large, secret or not equipment.
SKIP = re.compile(r"/(LogServices|Entries|Sessions|Accounts|Roles|Certificates|CertificateLocations|JsonSchemas|"
                  r"Schemas|Registries|Subscriptions|SubProcessors)(/|$)")
# Composition templates (AMI: /Systems/Capabilities) and pending-settings copies (AMI: /Systems/Self/SD) are typed
# like the real resource but are not its current state; links inside these annotations are not followed.
TEMPLATES = ("@Redfish.CollectionCapabilities", "@Redfish.Settings")
OEM = re.compile(r"(^|/)Oem/")
ACTIONS = re.compile(r"(^|/)Actions/")  # operations (standard or Oem), not properties
ACTION = re.compile(r"^(?:.*/)?Actions/(?:[^#]*/)?(#[^/]+)/(.+)$")  # an action's name, and one of its properties
ALLOWED = re.compile(r"^(\w+)@Redfish\.AllowableValues/\d+$")  # one allowed value of an action parameter
OEM_PATH = re.compile(r"(?:(.*)/)?Oem/([^/]+)/?(.*)")  # before Oem, vendor, vendor property
WILDCARD = re.compile(r"\\\{(\w+)\\\}")  # {Name} in a re.escape'd MetricProperties URI
# The reason inside a requests error message, without host, URL or object reprs.
REASON = re.compile(r"\[Errno -?\d+\] [^'\"()]+|(?:Read )?timed out|certificate verify failed[^'\"(]*"
                    r"|\d{3} \w+ Error: .*?(?= for url)|too many \d+ error responses")
SCHEMAS = Path(__file__).with_name("schemas")  # DMTF JSON Schemas, fetched once, then read offline
# The site: the folder of one fleet, prod (the default) or lab with SITE=lab. It holds the fleet's inventory, baseline
# and image catalog, and what the tools write about it.
SITE = Path(__file__).parent / os.environ.get("SITE", "prod")
INVENTORY_FILE = SITE / "inventory.yaml"  # the BMCs and how to reach them
BASELINE = SITE / "baseline.yaml"  # the approved firmware version per model and component
IMAGES = SITE / "images.yaml"  # the firmware image files kept per model, component and version
RUNS = SITE / "runs"  # the record of each rollout run: runs/<run id>.jsonl, one line per event
ROLLOUT = SITE / "rollout.yaml"  # how firmware rolls out on the site: canary, waves, gates
# The rollout policy when the site has none, each value overridable in <site>/rollout.yaml or by an option:
#   canary_per_model  hosts of each hardware model that go first (inventory hosts marked canary: true first)
#   waves             after the canary: the cumulative % of the other hosts done after each wave; it ends with 100
#   max_per_rack      hosts of one rack in the same wave, at most (0: no limit); the rest wait for a later wave
#   strict_waves      the first waves after the canary that, like it, halt on any failure
#   halt_at           later waves: halt when more than this share of the hosts tried failed
#   max_parallel      updates running at once in a wave (0: the whole wave)
#   soak              seconds to wait after a wave, then re-check its hosts, before the next one (0: none)
ROLLOUT_DEFAULTS = {"canary_per_model": 1, "waves": [5, 25, 100], "max_per_rack": 1, "strict_waves": 1,
                    "halt_at": 0.02, "max_parallel": 50, "soak": 1800}
SNAPSHOTS = SITE / "snapshots"  # the latest crawl of each host, read by the views
PREVIOUS = SNAPSHOTS / "previous"  # the crawl before it, for the diff view
DMTF = "https://redfish.dmtf.org/schemas/v1/"
KIND = {"Cel": "Temperature", "W": "Power", "kW": "Power", "V": "Voltage", "A": "Current", "RPM": "Fan speed",
        "{rev}/min": "Fan speed", "kW.h": "Energy", "J": "Energy", "L/min": "Liquid flow", "L/s": "Liquid flow",
        "Pa": "Pressure", "kPa": "Pressure", "Hz": "Frequency", "%": "Percent", "Percent": "Percent"}
# Limits by property names
LIMIT = re.compile(r"Threshold|Range|Capacity|Limit|Allocated|Requested|Available|Rated|TDP|SetPoint|Throttling")
CRITICAL = "UpperThresholdCritical|Thresholds/UpperCritical/Reading"
# Firmware of parts, one column per part model
FW_PARTS = (("Drive", "Drive", "Model", r"Revision"),
            ("Controller", "StorageController", "Model", r"FirmwareVersion"),
            ("Controller", "Storage/StorageControllers", "Model", r"FirmwareVersion"),
            ("NIC", "NetworkAdapter", "Model|Id", r"Controllers/\d+/FirmwarePackageVersion"),
            ("PSU", "PowerSupply", "Model", r"FirmwareVersion"),
            ("PSU", "Power/PowerSupplies", "Model", r"FirmwareVersion"))
JOB_TYPES = ("Job", "Task")  # JobService jobs and TaskService tasks: updates and other long-running operations
# Changes on every crawl, so not worth reporting in the diff: clocks and timestamps (readings are numbers).
VOLATILE = re.compile(r"(DateTime|Timestamp|Time)$")

SERVICE = ("Vendor", "Product", "RedfishVersion", "UUID")
SYSTEM = ("Manufacturer", "Model", "SKU", "SerialNumber", "PartNumber", "UUID", "BiosVersion", "HostName",
          "SystemType", "PowerState", "BootProgress/LastState", "Status/State", "Status/Health",
          "Status/HealthRollup", "ProcessorSummary/Count", "ProcessorSummary/Model",
          "MemorySummary/TotalSystemMemoryGiB")
MANAGER = ("ManagerType", "Manufacturer", "Model", "FirmwareVersion", "UUID", "DateTime", "Status/State",
           "Status/Health")
CHASSIS = ("ChassisType", "Manufacturer", "Model", "SerialNumber", "PartNumber", "SKU", "AssetTag",
           "PowerState", "Status/State", "Status/Health", "Status/HealthRollup")

PROPS = ("Property", "Value")
READINGS = ("Resource", "Name", "Property", "Role", "Value", "Units", "Critical", "Precision", "Definition", "Health",
            "State", "Source")
FIELDS = ("resource", "name", "property", "role", "value", "unit", "critical", "precision", "definition", "health",
          "state", "source")
DISCRETE = ("Health", "HealthRollup", "State")  # the discrete values of a Redfish Status, shown exactly as reported
STATUS_LEAF = re.compile(r"^(?:(.*)/)?Status/(?:Health|HealthRollup|State)$")  # path of an object that has a Status
INVENTORY_ROW = ("Host", "Vendor", "Project", "Manufacturer", "Model", "Serial", "UUID", "CPUs", "Cores", "GPUs",
                 "Memory GiB", "DIMMs", "Drives", "NICs", "PSUs")
TELEMETRY_ROW = ("Host", "Vendor", "Project", "Readings", "Hottest", "°C", "Critical", "Margin", "Power W",
                 "Not OK")
SENSORS = ("Kind", "Resource", "Name", "Property", "Role", "Value", "Units", "Critical", "Margin", "Health", "State",
           "Source")
COMPLIANCE_ROW = ("Host", "Vendor", "Project", "Model", "On baseline", "Needs")
JOBS_ROW = ("Host", "Resource", "Name", "State", "Status", "Message", "Time")
PREFLIGHT_ROW = ("Host", "Project", "Model", "Updateable", "A/B bank", "Rollback", "Running", "Baseline", "Direction",
                 "Verdict", "Reasons", "Update methods")
PLAN_ROW = ("Wave", "Hosts", "Members")
HOST_ROW = ("Component", "SoftwareId", "Running", "Baseline", "Direction", "Updateable", "Push", "Pull", "A/B bank",
            "Rollback", "Verdict", "Reasons")
# Job and task states of work still going on: pre-flight skips the host for now. Any other state but Completed
# (Exception, Killed, Cancelled, Interrupted, UserIntervention...) blocks it until a person looks.
RUNNING_STATES = {"New", "Starting", "Running", "Suspended", "Pending", "Stopping", "Service", "Continue", "Validating",
                  "Cancelling"}
DOTTED = re.compile(r"\d+(?:\.\d+)*")  # DotIntegerNotation, 2.86.86.86: also how versions without a scheme compare
# SemVer (Semantic Versioning 2.0): major.minor.patch, then an optional pre-release (-rc.1) and build metadata (+b7)
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")
ACTIONS_ROW = ("Wave", "Host", "Component", "Change", "Drain", "Update", "Reset", "Rollback")
VERSIONS_ROW = ("", "Version", "Role", "Planned hosts on it", "Package in the catalog")
# The report, for people: what needs a person, each wave's hosts, the hosts left out of the plan, a host's steps
ACTION_ROW = ("Host", "Wave", "Result", "Why", "Next")
WAVE_ROW = ("Wave", "Host", "Result", "Versions", "Took", "Why")
LEFT_OUT_ROW = ("Host", "Verdict", "Running", "Target", "Why")
STEP_ROW = ("Step", "Started (UTC)", "Took", "Result", "Detail")
REPORT_SCHEMA = "rollout-report/1"  # the version of report.json's layout, for the services that read it
# What a host that didn't end updated needs next: these make the report's "action needed"
NEXT_STEP = {"failed": "the old firmware still runs: read the task's messages before trying again",
             "rolled_back": "back on the version from before, verified: find out why the new one failed first",
             "needs_attention": "not verified: check the BMC answers and which version it runs, then give it back",
             "blocked": "pre-flight blocked it: clear the reasons, then plan again",
             "incomplete": "the run stopped during it: check the BMC before anything else"}
# The reset types that activate new firmware, most preferred first: graceful before forced
RESET_PREFERENCE = {"Manager": ("GracefulRestart", "ForceRestart"),
                    "ComputerSystem": ("GracefulRestart", "ForceRestart", "PowerCycle"),
                    "Chassis": ("PowerCycle", "FullPowerCycle", "ForceRestart")}
# The end states of a host that count against the rollout: halting looks at them after every wave
FAILED_STATES = {"failed", "rolled_back", "needs_attention"}
CAPABILITIES_ROW = ("Model", "Hosts", "Sample", "Redfish", "Types", "Services", "Actions", "SimpleUpdate",
                    "Push update", "Telemetry", "Sensors")
# Pseudo-column "@resource": the resource URI (with a JSON pointer for an item of an inline array). Other columns are
# headed by their property path.
HEADERS = {"@resource": "Resource"}
STYLE = {"OK": "green", "Enabled": "green", "Connected": "green", "Warning": "yellow", "Critical": "bold red",
         "Failed": "bold red", "Stopped": "bold red",
         "go": "green", "skip": "yellow", "block": "bold red",  # pre-flight verdicts
         "updated": "green", "rolled_back": "yellow", "needs_attention": "bold red", "failed": "bold red",  # run
         "passed": "green", "done": "green", "Completed": "green", "blocked": "bold red", "running": "yellow",
         "halted": "bold red", "incomplete": "yellow", "skipped": "yellow", "untouched": "dim", "Exception": "bold red"}
# What BIOS/BMC firmware reports instead of a real value (compared case-insensitively); skipped in the summary.
PLACEHOLDERS = {"", "-", "n/a", "na", "none", "unknown", "not available", "not specified", "default string",
                "to be filled by o.e.m.", "system manufacturer", "system product name", "no dimm", "nil"}
SEVERITY = {"Critical": 0, "Warning": 1}
SUMMARY = ("Host", "Vendor", "Project", "Connection", "Resources", "Errors", "Seconds", "RedfishVersion",
           "Manufacturer", "Model", "Health", "Error")
# No answer at all, as opposed to an error answer: connection refused or dropped, TLS failure, timeout, retries used up.
SILENT = (requests.ConnectionError, requests.Timeout, requests.exceptions.RetryError)
# Faults the lab injects into an update (rollout.py run --fault [HOST=]KIND), each where a real one would show
FAULTS = {"unhealthy": "the new firmware runs, then the post-check fails, as a health regression would",
          "rejected": "the payload is cut to 8 MiB on its way to the BMC, which refuses it",
          "silent-fail": "the BMC gets the running version's package as the target's: it takes it, the version stays",
          "no-return": "a 45 s reset timeout, shorter than the BMC's boot, so it isn't back in time"}
