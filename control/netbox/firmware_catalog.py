"""The firmware catalog as a view in NetBox: one firmware_image object per image in images.yaml and per approved
version in baseline.yaml, each linked to its hosts. A NetBox script, so nothing outside NetBox needs a token that
writes: the data source pulls git, and seed.py's event rule runs this after each of its syncs. Git stays the authority:
an edit here is undone by the next sync, and the rollout reads the files, never these objects."""
import re

import yaml
from core.models import DataFile, DataSource
from dcim.models import Device, Manufacturer, Site
from extras.scripts import Script
from netbox_custom_objects.models import CustomObjectType

SOURCE = "firmware catalog"
CATALOG = re.compile(r"control/catalog/([^/]+)/(images|baseline)\.yaml")
IMAGE_FIELDS = {"file", "sha256"}  # the object type has a field for each, and a new one in images.yaml stops the sync
# The Manufacturer a BMC reports, which starts a model's name in the catalog, and that vendor's name in NetBox. The
# lab's emulated GB200 NVL reports none, so its model reads "- -"
VENDORS = {"ASUSTeK COMPUTER INC.": "ASUS", "Dell Inc.": "DELL", "Giga Computing": "Gigabyte", "HPE": "HPE",
           "Supermicro": "Supermicro", "-": "NVIDIA"}


def catalog_rows(files) -> dict:
    """{path: text} of the catalog's files to {name: the object's fields}. Everything is read as text, as the tools read
    it, so 2.20 stays 2.20. Nothing is written until all of it has parsed."""
    sites, rows = {}, {}
    for path, text in files.items():
        if found := CATALOG.fullmatch(path):
            sites.setdefault(found[1], {})[found[2]] = yaml.load(text, Loader=yaml.BaseLoader) or {}
    for site, catalog in sites.items():
        images, baseline = catalog.get("images", {}), catalog.get("baseline", {})
        for model in images.keys() | baseline.keys():
            approved = baseline.get(model, {})
            for component in images.get(model, {}).keys() | approved.keys():
                versions = images.get(model, {}).get(component, {})
                for version in versions.keys() | ({approved[component]} if component in approved else set()):
                    image = versions.get(version, {})
                    if unknown := image.keys() - IMAGE_FIELDS:
                        raise ValueError(f"{site}, {model}, {component} {version}: firmware_image has no field for "
                                         f"{sorted(unknown)}: add it in control/netbox/seed.py")
                    rows[f"{site}: {model} / {component} / {version}"] = {
                        "site": site, "catalog_model": model, "component": component, "version": version,
                        "file": image.get("file", ""), "sha256": image.get("sha256", ""),
                        "approved": approved.get(component) == version}
    return rows


def split_model(catalog_model) -> tuple[str | None, str]:
    """A catalog model to its vendor in NetBox, None when VENDORS doesn't have it, and its model"""
    for reported, vendor in VENDORS.items():
        if catalog_model.startswith(f"{reported} "):
            return vendor, catalog_model.removeprefix(f"{reported} ")
    return None, catalog_model


def is_host_of(device, site, vendor, model) -> bool:
    """Its site and vendor, and its model unless one side doesn't know it. NetBox records no firmware per device, so
    every component of a model maps to the same hosts."""
    # ponytail: prod's device types are all "unspecified", so each Dell model's rows list all 3 Dell hosts; a real
    # device type on a device narrows it here, with no change
    return (device.site.slug == site and device.device_type.manufacturer.name == vendor
            and (model == "-" or device.device_type.model in ("unspecified", model)))


def sync_catalog(source, warn=print) -> tuple[int, int, int]:
    """Make the firmware_image objects equal the source's catalog files, writing only what differs. Returns how many
    were created, updated and deleted."""
    rows = catalog_rows({f.path: f.data_as_string or "" for f in DataFile.objects.filter(source=source)})
    Image = CustomObjectType.objects.get(name="firmware_image").get_model()
    current = {o.image: o for o in Image.objects.all()}
    devices = list(Device.objects.select_related("site", "device_type__manufacturer"))
    vendors, sites = {m.name: m for m in Manufacturer.objects.all()}, {s.slug: s for s in Site.objects.all()}
    created = updated = 0
    for name, row in rows.items():
        vendor, model = split_model(row["catalog_model"])
        if vendor is None or row["site"] not in sites:
            warn(f"{name}: no hosts: NetBox has no site {row['site']}, or VENDORS in firmware_catalog.py no vendor "
                 f"for it")
        hosts = [d for d in devices if is_host_of(d, row["site"], vendor, model)]
        fields = {**row, "site": sites.get(row["site"]), "vendor": vendors.get(vendor)}
        found = current.pop(name, None)
        target = found or Image(image=name)
        changed = not found or any(getattr(found, k) != v for k, v in fields.items())
        if changed:
            for key, value in fields.items():
                setattr(target, key, value)
            target.save()
        relinked = not found or set(target.hosts.all()) != set(hosts)
        if relinked:
            target.hosts.set(hosts)
        created += not found
        updated += bool(found) and (changed or relinked)
    for stale in current.values():
        stale.delete()
    return created, updated, len(current)


class FirmwareCatalogSync(Script):
    class Meta:
        name = "Firmware catalog sync"
        description = "Make the firmware_image objects equal the firmware catalog in git"

    def run(self, data, commit):
        created, updated, deleted = sync_catalog(DataSource.objects.get(name=SOURCE), self.log_warning)
        return f"{created} created, {updated} updated, {deleted} deleted"
