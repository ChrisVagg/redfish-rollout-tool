"""Loads lab/inventory.yaml's servers into NetBox once: a site that already has devices is left as it is, so NetBox stays
the source and its edits stay. Also makes the tools' account, redfish, which may only view devices, and its read-only
API token from NETBOX_TOKEN in netbox/.netbox.env, so it survives a new NetBox volume. Runs in NetBox's Django shell:
make netbox-up.

A server becomes a device of site lab: named after its node, else its BMC address; its vendor the manufacturer, its
project the tenant, its rack the rack, its model the device type (unspecified without one), canary: true the tag
canary; its role server. The device is the server and the BMC how it's reached: its management-only interface bmc,
which holds the BMC's address, the device's out-of-band IP."""
import os

import yaml
from django.utils.text import slugify

from core.models import ObjectType
from ipam.models import IPAddress
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Rack, Site
from extras.models import Tag
from tenancy.models import Tenant
from users.models import ObjectPermission, Token, User

# The tools' account, redfish: it may only view devices, can't log in, and its token is read-only too
tools, created = User.objects.get_or_create(username="redfish")
if created:
    tools.set_unusable_password()
    tools.save()
view, _ = ObjectPermission.objects.get_or_create(name="redfish: view devices", defaults={"actions": ["view"]})
view.object_types.set([ObjectType.objects.get_for_model(Device)])
view.users.add(tools)
key, value = os.environ["NETBOX_TOKEN"].removeprefix("nbt_").split(".")
Token.objects.filter(key=key).exclude(user=tools).delete()  # made for another account before
if not Token.objects.filter(key=key).exists():
    Token.objects.create(user=tools, key=key, token=value, write_enabled=False, description="redfish tools: read-only")

canary, _ = Tag.objects.get_or_create(slug="canary", defaults={"name": "canary"})

site, _ = Site.objects.get_or_create(slug="lab", defaults={"name": "lab"})
if Device.objects.filter(site=site).exists():
    print(f"NetBox has {Device.objects.filter(site=site).count()} devices in site lab: it is the source, nothing loaded")
else:
    servers = yaml.safe_load(open("/seed/lab/inventory.yaml"))["servers"]
    role, _ = DeviceRole.objects.get_or_create(slug="server", defaults={"name": "server"})
    for s in servers:
        maker, _ = Manufacturer.objects.get_or_create(slug=slugify(s["vendor"]), defaults={"name": s["vendor"]})
        model = s.get("model", "unspecified")
        kind, _ = DeviceType.objects.get_or_create(manufacturer=maker, slug=f"{maker.slug}-{slugify(model)}",
                                                   defaults={"model": model})
        tenant = s.get("project") and Tenant.objects.get_or_create(
            slug=slugify(s["project"]), defaults={"name": s["project"]})[0]
        rack = s.get("rack") and Rack.objects.get_or_create(site=site, name=s["rack"])[0]
        device = Device.objects.create(name=s.get("node") or s["host"], site=site, role=role, device_type=kind,
                                       tenant=tenant or None, rack=rack or None)
        bmc = Interface.objects.create(device=device, name="bmc", type="1000base-t", mgmt_only=True)
        device.oob_ip = IPAddress.objects.create(address=f"{s['host']}/32", assigned_object=bmc)
        device.save()
        if s.get("canary"):
            device.tags.add(canary)
    print(f"Loaded the {len(servers)} servers of lab/inventory.yaml into NetBox, site lab")
