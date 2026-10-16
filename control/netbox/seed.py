"""Seeds NetBox, the inventory's only source, in its Django shell: make control-up. It makes only what is missing, so
NetBox's own edits stay:
  the services' accounts, redfish_poller_sa, redfish_exporter_sa and redfish_rollout_sa: each may only view devices,
    can't log in, and has one API token, read-only, NETBOX_TOKEN_<SERVICE> in control/netbox/.tokens.env, so it
    survives a new NetBox volume
  the sites lab and prod, the tags canary and writable, and each site's config context: how its BMCs are reached
  the lab's ten BMCs, when site lab has no device: bmc1 to bmc10 on 127.1.0.N, as lab/docker-compose.yaml runs them,
    NVIDIA GB200 NVL in racks r1 to r3 in turn, each tagged writable, bmc1 also canary
A device is the server and its BMC how it's reached: its management-only interface bmc holds the BMC's address, the
device's out-of-band IP. Prod's servers are added in NetBox (PROD.md), each with the platform whose config context
names its BMC account."""
import os

from core.models import ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Rack, Site
from extras.models import ConfigContext, Tag
from ipam.models import IPAddress
from tenancy.models import Tenant
from users.models import ObjectPermission, Token, User

# Each service's account: it may only view devices, can't log in, and its one token is read-only
for service in ("poller", "exporter", "rollout"):
    account, created = User.objects.get_or_create(username=f"redfish_{service}_sa")
    if created:
        account.set_unusable_password()
        account.save()
    view, _ = ObjectPermission.objects.get_or_create(name=f"redfish_{service}_sa: view devices",
                                                     defaults={"actions": ["view"]})
    view.object_types.set([ObjectType.objects.get_for_model(Device)])
    view.users.add(account)
    key, value = os.environ[f"NETBOX_TOKEN_{service.upper()}"].removeprefix("nbt_").split(".")
    # the account's one token is the file's: another account's with its key, an older one, or its key with another
    # secret (a rotation) goes; and it stays read-only, whatever was changed in NetBox since
    for token in Token.objects.filter(user=account) | Token.objects.filter(key=key):
        if token.user != account or token.key != key or not token.validate(value):
            token.delete()
    if not Token.objects.filter(key=key).exists():
        Token.objects.create(user=account, key=key, token=value, write_enabled=False,
                             description=f"redfish {service}: read-only")
    Token.objects.filter(key=key).update(write_enabled=False)

# The sites, and how each one's BMCs are reached: its config context. Without one, HTTPS with the certificate checked.
# The lab's account is OpenBMC's default, its values in LAB_USERNAME and LAB_PASSWORD (make)
canary, _ = Tag.objects.get_or_create(slug="canary", defaults={"name": "canary"})
writable, _ = Tag.objects.get_or_create(slug="writable", defaults={"name": "writable"})
for name, data in {"lab": {"verify": False, "retries": 1, "deadline": 300, "username_env": "LAB_USERNAME",
                           "password_env": "LAB_PASSWORD"},
                   "prod": {"verify": False}}.items():
    site, _ = Site.objects.get_or_create(slug=name, defaults={"name": name})
    context, created = ConfigContext.objects.get_or_create(name=f"site {name}", defaults={"data": data})
    if created:
        context.sites.set([site])

lab = Site.objects.get(slug="lab")
if count := Device.objects.filter(site=lab).count():
    print(f"NetBox has {count} devices in site {lab}: it is the source, nothing loaded")
else:
    nvidia, _ = Manufacturer.objects.get_or_create(slug="nvidia", defaults={"name": "NVIDIA"})
    kind, _ = DeviceType.objects.get_or_create(manufacturer=nvidia, slug="nvidia-gb200-nvl",
                                               defaults={"model": "GB200 NVL"})
    role, _ = DeviceRole.objects.get_or_create(slug="server", defaults={"name": "server"})
    tenant, _ = Tenant.objects.get_or_create(slug="lab", defaults={"name": "lab"})
    for n in range(1, 11):
        rack, _ = Rack.objects.get_or_create(site=lab, name=f"r{(n - 1) % 3 + 1}")
        device = Device.objects.create(name=f"bmc{n}", site=lab, role=role, device_type=kind, tenant=tenant, rack=rack)
        bmc = Interface.objects.create(device=device, name="bmc", type="1000base-t", mgmt_only=True)
        device.oob_ip = IPAddress.objects.create(address=f"127.1.0.{n}/32", assigned_object=bmc)
        device.save()
        device.tags.add(writable, *([canary] if n == 1 else []))
    print(f"Added the lab's 10 BMCs to NetBox, site {lab}")
