"""make catalog-test: the firmware catalog's sync in NetBox itself. It runs in NetBox's Django shell: a throwaway git
repository, a real DataSource.sync() of it, the real object type. All of it in one transaction that is always rolled
back, so NetBox keeps nothing."""
import tempfile
from pathlib import Path

from core.models import DataSource
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import transaction
from dulwich import porcelain
from netbox_custom_objects.models import CustomObjectType
from scripts import firmware_catalog
from scripts.firmware_catalog import IMAGE_FIELDS, SOURCE

IMAGES_FILE, BASELINE_FILE = "control/catalog/cata/images.yaml", "control/catalog/cata/baseline.yaml"
IMAGES = '''
"Dell Inc. PowerEdge R630":
  "System BIOS":
    "2.19.0": {file: "images/r630-2.19.0.bin", sha256: "aaaa"}
    "2.20": {file: "images/r630-2.20.bin", sha256: "bbbb"}
"- -":
  "Manager (BMC)":
    "3.1.0": {file: "https://cache/gb200.tar", sha256: "cccc"}
'''
BASELINE = '''
"Dell Inc. PowerEdge R630":
  "System BIOS": 2.20
  "PSU DELTA   ": "00.14.7A"
"- -":
  "Manager (BMC)": "3.1.0"
"Unknown Corp X1":
  "System BIOS": "1.0"
"Nowhere Inc. X1":
  "System BIOS": "1.0"
'''
BIOS_OLD = "cata: Dell Inc. PowerEdge R630 / System BIOS / 2.19.0"
BIOS_NEW = "cata: Dell Inc. PowerEdge R630 / System BIOS / 2.20"
PSU, BMC = "cata: Dell Inc. PowerEdge R630 / PSU DELTA    / 00.14.7A", "cata: - - / Manager (BMC) / 3.1.0"
UNKNOWN = "cata: Unknown Corp X1 / System BIOS / 1.0"
NO_DEVICES = "cata: Nowhere Inc. X1 / System BIOS / 1.0"


def sync_catalog(source, warn=lambda _: None):
    return firmware_catalog.sync_catalog(source, warn)


def commit(repo, files):
    """Each file's new text, None to delete it"""
    for name, text in files.items():
        path = repo / name
        if text is None:
            porcelain.remove(str(repo), paths=[str(path)])
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            porcelain.add(str(repo), paths=[str(path)])
    porcelain.commit(str(repo), message=b"catalog", author=b"t <t@t>", committer=b"t <t@t>")


def rows():
    return {o.image: o for o in CustomObjectType.objects.get(name="firmware_image").get_model().objects.all()}


def hosts(row):
    return {d.name for d in row.hosts.all()}


def fields(row):
    return (row.site.slug, row.vendor.name if row.vendor else None, row.catalog_model, row.component, row.version,
            row.file, row.sha256, row.approved)


def device(name, site, vendor, model):
    maker, _ = Manufacturer.objects.get_or_create(name=vendor, defaults={"slug": vendor.lower()})
    kind, _ = DeviceType.objects.get_or_create(manufacturer=maker, model=model,
                                               defaults={"slug": model.lower().replace(" ", "-")})
    role, _ = DeviceRole.objects.get_or_create(slug="server", defaults={"name": "server"})
    return Device.objects.create(name=name, site=site, role=role, device_type=kind)


def refused(source, repo, files, says):
    """A bad commit stops the sync with why, and leaves every object as it was"""
    before = {k: fields(v) for k, v in rows().items()}
    commit(repo, files)
    source.sync()
    try:
        sync_catalog(source)
    except Exception as e:
        assert says in str(e), e
    else:
        raise AssertionError(f"accepted a bad catalog: {files}")
    assert {k: fields(v) for k, v in rows().items()} == before


def run(repo):
    firmware_catalog.VENDORS["Nowhere Inc."] = "Nowhere"
    source = DataSource.objects.create(name="catalog test", type="git", source_url=str(repo),
                                       ignore_rules=DataSource.objects.get(name=SOURCE).ignore_rules)
    for row in rows().values():  # the real ones, until the rollback
        row.delete()
    site, other = (Site.objects.create(name=s, slug=s) for s in ("cata", "catb"))
    device("any-dell", site, "DELL", "unspecified")
    device("r630", site, "DELL", "PowerEdge R630")
    device("r730", site, "DELL", "PowerEdge R730xd")
    device("gb200", site, "NVIDIA", "GB200 NVL")
    device("asus", site, "ASUS", "unspecified")
    device("elsewhere", other, "DELL", "unspecified")

    # The object type has a field for each of git's
    names = {f.name for f in CustomObjectType.objects.get(name="firmware_image").fields.all()}
    assert IMAGE_FIELDS | {"site", "catalog_model", "vendor", "component", "version", "approved", "hosts"} <= names, \
        names

    # Only the catalog comes in: the ignore rules leave the rest of the repository out of NetBox's database
    commit(repo, {IMAGES_FILE: IMAGES, BASELINE_FILE: BASELINE, "README.md": "x", "constants.py": "x",
                  "lab/policy.yaml": "x", "control/netbox/seed.py": "x", "control/docker-compose.yaml": "x"})
    source.sync()
    assert {f.path for f in source.datafiles.all()} == {IMAGES_FILE, BASELINE_FILE}, "the ignore rules"

    # Every row equals git, field by field: images, approved versions with no image, text left as text
    warnings = []
    counts = sync_catalog(source, warnings.append)
    assert counts == (6, 0, 0), counts
    got = rows()
    assert {k: fields(v) for k, v in got.items()} == {
        BIOS_OLD: ("cata", "DELL", "Dell Inc. PowerEdge R630", "System BIOS", "2.19.0", "images/r630-2.19.0.bin",
                   "aaaa", False),
        BIOS_NEW: ("cata", "DELL", "Dell Inc. PowerEdge R630", "System BIOS", "2.20", "images/r630-2.20.bin", "bbbb",
                   True),
        PSU: ("cata", "DELL", "Dell Inc. PowerEdge R630", "PSU DELTA   ", "00.14.7A", "", "", True),
        BMC: ("cata", "NVIDIA", "- -", "Manager (BMC)", "3.1.0", "https://cache/gb200.tar", "cccc", True),
        UNKNOWN: ("cata", None, "Unknown Corp X1", "System BIOS", "1.0", "", "", True),
        NO_DEVICES: ("cata", None, "Nowhere Inc. X1", "System BIOS", "1.0", "", "", True),
    }, {k: fields(v) for k, v in got.items()}

    # Mapped by site and vendor, and by model where NetBox knows it. A vendor it can't place maps to nothing and says
    # so; a known vendor NetBox has no device of yet, as on a fresh clone, is no problem and stays quiet
    assert hosts(got[BIOS_NEW]) == hosts(got[PSU]) == {"any-dell", "r630"}, hosts(got[BIOS_NEW])
    assert hosts(got[BMC]) == {"gb200"}, hosts(got[BMC])
    assert hosts(got[UNKNOWN]) == hosts(got[NO_DEVICES]) == set(), hosts(got[NO_DEVICES])
    assert len(warnings) == 1 and UNKNOWN in warnings[0], warnings

    # Nothing changed, nothing is written
    stamps = {k: v.last_updated for k, v in rows().items()}
    assert sync_catalog(source) == (0, 0, 0)
    assert {k: v.last_updated for k, v in rows().items()} == stamps

    # A new host is linked on the next sync, without a commit
    device("r630-new", site, "DELL", "unspecified")
    assert sync_catalog(source)[1] == 3
    assert hosts(rows()[BIOS_NEW]) == {"any-dell", "r630", "r630-new"}

    # An edit in NetBox doesn't survive: git wins
    edited = rows()[BIOS_NEW]
    edited.sha256 = "edited"
    edited.save()
    assert sync_catalog(source) == (0, 1, 0) and rows()[BIOS_NEW].sha256 == "bbbb"

    # A commit moves the approval, and an image gone from git goes from NetBox
    commit(repo, {BASELINE_FILE: BASELINE.replace("2.20", "2.19.0"),
                  IMAGES_FILE: IMAGES.replace('    "3.1.0": {file: "https://cache/gb200.tar", sha256: "cccc"}\n',
                                              '    "3.2.0": {file: "https://cache/gb200-2.tar", sha256: "dddd"}\n')})
    source.sync()
    created, updated, deleted = sync_catalog(source)
    got = rows()
    assert got[BIOS_OLD].approved and not got[BIOS_NEW].approved, "the approval didn't move"
    added = got["cata: - - / Manager (BMC) / 3.2.0"]
    assert added.sha256 == "dddd" and not added.approved, fields(added)
    assert got[BMC].file == "" and got[BMC].approved, "still approved, with its image gone"
    assert (created, updated, deleted) == (1, 3, 0), (created, updated, deleted)

    # A bad commit stops the sync and changes nothing: a field the schema lacks, then YAML that doesn't parse
    refused(source, repo, {IMAGES_FILE: IMAGES.replace('sha256: "aaaa"', 'sha256: "aaaa", signature: "x"')},
            "signature")
    refused(source, repo, {IMAGES_FILE: 'a: [b'}, "expected")

    # A site's catalog removed from git is removed from NetBox
    commit(repo, {IMAGES_FILE: None, BASELINE_FILE: None})
    source.sync()
    assert sync_catalog(source)[2] == len(got) and not rows(), "deleted rows stayed"


class RollBack(Exception):
    pass


repo = Path(tempfile.mkdtemp())
porcelain.init(str(repo))
try:
    with transaction.atomic():
        run(repo)
        raise RollBack
except RollBack:
    print("firmware catalog sync: all checks passed, nothing kept")
