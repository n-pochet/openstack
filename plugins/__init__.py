"""
    Copyright 2017 Inmanta

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    Contact: code@inmanta.com
"""

import os
import logging

from inmanta.execute.proxy import UnknownException
from inmanta.resources import resource, PurgeableResource, ManagedResource
from inmanta import resources
from inmanta.agent import handler
from inmanta.agent.handler import provider, SkipResource, cache, ResourceHandler, ResourcePurged, CRUDHandler
from inmanta.export import dependency_manager

from neutronclient.common import exceptions
from neutronclient.neutron import client as neutron_client
from neutronclient import neutron

from novaclient import client as nova_client
import novaclient.exceptions

from keystoneauth1.identity import v3
from keystoneauth1 import session
from keystoneclient.v3 import client as keystone_client

try:
    from keystoneclient.exceptions import NotFound
except ImportError:
    from keystoneclient.openstack.common.apiclient.exceptions import NotFound

# silence a logger
loud_logger = logging.getLogger("requests.packages.urllib3.connectionpool")
loud_logger.propagate = False


LOGGER = logging.getLogger(__name__)
NULL_UUID = "00000000-0000-0000-0000-000000000000"
NULL_ID = "00000000000000000000000000000000"


class OpenstackResource(PurgeableResource, ManagedResource):
    fields = ("project", "admin_user", "admin_password", "admin_tenant", "auth_url")

    @staticmethod
    def get_project(exporter, resource):
        return resource.project.name

    @staticmethod
    def get_admin_user(exporter, resource):
        return resource.provider.username

    @staticmethod
    def get_admin_password(exporter, resource):
        return resource.provider.password

    @staticmethod
    def get_admin_tenant(exporter, resource):
        return resource.provider.tenant

    @staticmethod
    def get_auth_url(exporter, resource):
        return resource.provider.connection_url


@resource("openstack::Host", agent="provider.name", id_attribute="name")
class Host(OpenstackResource):
    """
        A virtual machine managed by a hypervisor or IaaS
    """
    fields = ("name", "flavor", "image", "key_name", "user_data", "key_value", "ports", "security_groups")

    @staticmethod
    def get_key_name(exporter, vm):
        return vm.key_pair.name

    @staticmethod
    def get_key_value(exporter, vm):
        return vm.key_pair.public_key

    @staticmethod
    def get_user_data(exporter, vm):
        """
            Return an empty string when the user_data value is unknown
            TODO: this is a hack
        """
        try:
            ua = vm.user_data
        except UnknownException:
            ua = ""
        return ua

    @staticmethod
    def get_ports(_, vm):
        ports = []
        for p in vm.ports:
            port = {"name": p.name, "address": None, "network": p.subnet.name, "dhcp": p.dhcp, "index": p.port_index}
            try:
                port["address"] = p.address
            except UnknownException:
                pass
            ports.append(port)

        return ports

    @staticmethod
    def get_security_groups(_, vm):
        return [v.name for v in vm.security_groups]


@resource("openstack::Network", agent="provider.name", id_attribute="name")
class Network(OpenstackResource):
    """
        This class represents a network in neutron
    """
    fields = ("name", "external", "physical_network", "network_type", "segmentation_id")


@resource("openstack::Subnet", agent="provider.name", id_attribute="name")
class Subnet(OpenstackResource):
    """
        This class represent a subnet in neutron
    """
    fields = ("name", "network_address", "dhcp", "allocation_start", "allocation_end", "network")

    @staticmethod
    def get_network(_, subnet):
        return subnet.network.name


@resource("openstack::Router", agent="provider.name", id_attribute="name")
class Router(OpenstackResource):
    """
        This class represent a router in neutron
    """
    fields = ("name", "subnets", "gateway", "ports", "routes")

    @staticmethod
    def get_gateway(_, router):
        if hasattr(router.ext_gateway, "name"):
            return router.ext_gateway.name

        return ""

    @staticmethod
    def get_routes(_, router):
        routes = {route.destination: route.nexthop for route in router.routes}
        return routes

    @staticmethod
    def get_subnets(_, router):
        return sorted([subnet.name for subnet in router.subnets])

    @staticmethod
    def get_ports(_, router):
        return [p.name for p in router.ports]


@resource("openstack::RouterPort", agent="provider.name", id_attribute="name")
class RouterPort(OpenstackResource):
    """
        A port in a router
    """
    fields = ("name", "address", "subnet", "router", "network")

    @staticmethod
    def get_subnet(_, port):
        return port.subnet.name

    @staticmethod
    def get_network(_, port):
        return port.subnet.network.name

    @staticmethod
    def get_router(_, port):
        return port.router.name


@resource("openstack::HostPort", agent="provider.name", id_attribute="name")
class HostPort(OpenstackResource):
    """
        A port in a router
    """
    fields = ("name", "address", "subnet", "host", "network", "portsecurity", "dhcp", "port_index")

    @staticmethod
    def get_address(exporter, port):
        try:
            return port.address
        except UnknownException:
            return ""

    @staticmethod
    def get_subnet(_, port):
        return port.subnet.name

    @staticmethod
    def get_network(_, port):
        return port.subnet.network.name

    @staticmethod
    def get_host(_, port):
        return port.host.name


@resource("openstack::SecurityGroup", agent="provider.name", id_attribute="name")
class SecurityGroup(OpenstackResource):
    """
        A security group in an OpenStack tenant
    """
    fields = ("name", "description", "manage_all", "rules")

    @staticmethod
    def get_rules(exporter, group):
        rules = []
        for rule in group.rules:
            json_rule = {"protocol": rule.ip_protocol,
                         "direction": rule.direction}

            if rule.port > 0:
                json_rule["port_range_min"] = rule.port
                json_rule["port_range_max"] = rule.port

            else:
                json_rule["port_range_min"] = rule.port
                json_rule["port_range_max"] = rule.port

            if json_rule["port_range_min"] == 0:
                json_rule["port_range_min"] = None

            if json_rule["port_range_max"] == 0:
                json_rule["port_range_max"] = None

            try:
                json_rule["remote_ip_prefix"] = rule.remote_prefix
            except Exception:
                pass

            try:
                json_rule["remote_group"] = rule.remote_group.name
            except Exception:
                pass

            rules.append(json_rule)
        return rules


@resource("openstack::FloatingIP", agent="provider.name", id_attribute="name")
class FloatingIP(OpenstackResource):
    """
        A floating ip
    """
    fields = ("name", "port", "external_network")

    @staticmethod
    def get_port(_, fip):
        return fip.port.name

    @staticmethod
    def get_external_network(_, fip):
        return fip.external_network.name


class KeystoneResource(PurgeableResource):
    fields = ("admin_token", "url", "admin_user", "admin_password", "admin_tenant", "auth_url")

    @staticmethod
    def get_admin_token(_, resource):
        return resource.provider.token

    @staticmethod
    def get_url(_, resource):
        return os.path.join(resource.provider.admin_url, "v2.0/")

    @staticmethod
    def get_admin_user(exporter, resource):
        return resource.provider.username

    @staticmethod
    def get_admin_password(exporter, resource):
        return resource.provider.password

    @staticmethod
    def get_admin_tenant(exporter, resource):
        return resource.provider.tenant

    @staticmethod
    def get_auth_url(exporter, resource):
        return resource.provider.connection_url


@resource("openstack::Project", agent="provider.name", id_attribute="name")
class Project(KeystoneResource):
    """
        This class represents a project in keystone
    """
    fields = ("name", "enabled", "description", "manage")

    @staticmethod
    def get_project(exporter, resource):
        return resource.project.name


@resource("openstack::User", agent="provider.name", id_attribute="name")
class User(KeystoneResource):
    """
        A user in keystone
    """
    fields = ("name", "email", "enabled", "password")


@resource("openstack::Role", agent="provider.name", id_attribute="role_id")
class Role(KeystoneResource):
    """
        A role that adds a user to a project
    """
    fields = ("role_id", "role", "project", "user", "project")

    @staticmethod
    def get_project(exporter, resource):
        return resource.project.name

    @staticmethod
    def get_user(exporter, resource):
        return resource.user.name


@resource("openstack::Service", agent="provider.name", id_attribute="name")
class Service(KeystoneResource):
    """
        A service for which endpoints can be registered
    """
    fields = ("name", "type", "description")


@resource("openstack::EndPoint", agent="provider.name", id_attribute="service_id")
class EndPoint(KeystoneResource):
    """
        An endpoint for a service
    """
    fields = ("region", "internal_url", "public_url", "admin_url", "service_id")


@dependency_manager
def openstack_dependencies(config_model, resource_model):
    projects = {}
    networks = {}
    routers = {}
    subnets = {}
    vms = {}
    ports = {}
    fips = {}

    for _, res in resource_model.items():
        if res.id.entity_type == "openstack::Project":
            projects[res.name] = res

        elif res.id.entity_type == "openstack::Network":
            networks[res.name] = res

        elif res.id.entity_type == "openstack::Router":
            routers[res.name] = res

        elif res.id.entity_type == "openstack::Subnet":
            subnets[res.name] = res

        elif res.id.entity_type == "openstack::Host":
            vms[res.name] = res

        elif res.id.entity_type == "openstack::HostPort":
            ports[res.name] = res

        elif res.id.entity_type == "openstack::FloatingIP":
            fips[res.name] = res

    # they require the tenant to exist
    for network in networks.values():
        if network.model.project.name in projects:
            network.requires.add(projects[network.model.project.name])

    for router in routers.values():
        if router.model.project.name in projects:
            router.requires.add(projects[router.model.project.name])

        # depend on the attached subnets
        for subnet_name in router.subnets:
            router.requires.add(subnets[subnet_name])

    for subnet in subnets.values():
        subnet.requires.add(projects[subnet.model.project.name])

        # also require the network it is attached to
        subnet.requires.add(networks[subnet.model.network.name])

    for vm in vms.values():
        vm.requires.add(projects[vm.model.project.name])

        for port in vm.ports:
            vm.requires.add(subnets[port["network"]])

    for port in ports.values():
        port.requires.add(projects[port.model.project.name])
        port.requires.add(subnets[port.network])
        port.requires.add(vms[port.host])

    for fip in fips.values():
        fip.requires.add(networks[fip.external_network])
        fip.requires.add(ports[fip.port])


CRED_TIMEOUT = 600
RESOURCE_TIMEOUT = 10


class OpenStackHandler(CRUDHandler):

    @cache(timeout=CRED_TIMEOUT)
    def get_session(self, auth_url, project, admin_user, admin_password):
        auth = v3.Password(auth_url=auth_url, username=admin_user, password=admin_password, project_name=project,
                           user_domain_id="default", project_domain_id="default")
        sess = session.Session(auth=auth)
        return sess

    @cache(timeout=CRED_TIMEOUT)
    def get_nova_client(self, auth_url, project, admin_user, admin_password):
        return nova_client.Client("2", session=self.get_session(auth_url, project, admin_user, admin_password))

    @cache(timeout=CRED_TIMEOUT)
    def get_neutron_client(self, auth_url, project, admin_user, admin_password):
        return neutron_client.Client("2.0", session=self.get_session(auth_url, project, admin_user, admin_password))

    @cache(timeout=CRED_TIMEOUT)
    def get_keystone_client(self, auth_url, project, admin_user, admin_password):
        return keystone_client.Client(session=self.get_session(auth_url, project, admin_user, admin_password))

    def get_keystone(self, resource):
        return self.get_keystone_client(resource.auth_url, resource.project, resource.admin_user, resource.admin_password)

    def pre(self, ctx, resource):
        project = resource.admin_tenant
        self._nova = self.get_nova_client(resource.auth_url, project, resource.admin_user, resource.admin_password)
        self._neutron = self.get_neutron_client(resource.auth_url, project, resource.admin_user,
                                                resource.admin_password)
        self._keystone = self.get_keystone_client(resource.auth_url, project, resource.admin_user, resource.admin_password)

    def post(self, ctx, resource):
        self._nova = None
        self._neutron = None
        self._keystone = None

    def get_project_id(self, resource, name):
        """
            Retrieve the id of a project based on the given name
        """
        # Fallback for non admin users
        if resource.admin_tenant == name:
            session = self.get_session(resource.auth_url, resource.project, resource.admin_user, resource.admin_password)
            return session.get_project_id()

        try:
            tenant = self.get_keystone(resource).tenants.find(name=name)
            return tenant.id
        except Exception:
            return None

    def get_network_id(self, project_id, name):
        """
            Retrieve the network id based on the name of the network
        """
        if project_id is not None:
            networks = self._neutron.list_networks(tenant_id=project_id, name=name)
        else:
            networks = self._neutron.list_networks(name=name)

        if len(networks["networks"]) == 0:
            return None

        elif len(networks["networks"]) > 1:
            raise Exception("Found more than one network with name %s for project %s" % (name, project_id))

        else:
            return networks["networks"][0]["id"]

    def get_subnet_id(self, project_id, name):
        """
            Retrieve the subnet id based on the name of the network
        """
        subnets = self._neutron.list_subnets(tenant_id=project_id, name=name)

        if len(subnets["subnets"]) == 0:
            return None

        elif len(subnets["subnets"]) > 1:
            raise Exception("Found more than one subnet with name %s for project %s" % (name, project_id))

        else:
            return subnets["subnets"][0]["id"]

    def get_router_id(self, project_id, name):
        """
            Retrieve the router id based on the name of the network
        """
        routers = self._neutron.list_routers(name=name)

        if len(routers["routers"]) == 0:
            return None

        elif len(routers["routers"]) > 1:
            raise Exception("Found more than one router with name %s for project %s" % (name, project_id))

        else:
            return routers["routers"][0]["id"]

    def get_host_id(self, project_id, name):
        return self.get_host(project_id, name).id

    def get_host(self, project_id, name):
        """
            Retrieve the router id based on the name of the network
        """
        vms = self._nova.servers.findall(name=name)

        if len(vms) == 0:
            return None

        elif len(vms) > 1:
            raise Exception("Found more than one VM with name %s for project %s" % (name, project_id))

        else:
            return vms[0]

    def get_host_for_id(self, id):
        """
            Retrieve the router id based on the name of the network
        """
        vms = self._nova.servers.findall(id=id)

        if len(vms) == 0:
            return None

        elif len(vms) > 1:
            raise Exception("Found more than one VM with id %s" % (id))

        else:
            return vms[0]


@provider("openstack::Host", name="openstack")
class VMHandler(ResourceHandler):
    """
        This class handles managing openstack resources
    """
    __connections = {}

    def pre(self, ctx, resource):
        """
            Setup a connection with neutron
        """
        key = (resource.auth_url, resource.project, resource.admin_user, resource.admin_password)
        if key in VMHandler.__connections:
            self._client, self._neutron = VMHandler.__connections[key]
        else:
            auth = v2.Password(auth_url=resource.auth_url, username=resource.admin_user, password=resource.admin_password,
                               tenant_name=resource.project)
            sess = session.Session(auth=auth)
            self._client = nova_client.Client("2", session=sess)
            self._neutron = neutron_client.Client("2.0", session=sess)
            VMHandler.__connections[key] = (self._client, self._neutron)

    def post(self, ctx, resource):
        self._client = None
        self._neutron = None

    @cache(timeout=10)
    def get_vm(self, name):
        server = self._client.servers.list(search_opts={"name": name})

        if len(server) == 0:
            return None

        elif len(server) == 1:
            return server[0]

        else:
            raise Exception("Multiple virtual machines with name %s exist." % name)

    def check_resource(self, ctx, resource):
        """
            This method will check what the status of the give resource is on
            openstack.
        """
        current = resource.clone()
        server = self.get_vm(resource.name)

        if server is None:
            current.purged = True

        else:
            current.purged = False
            current.security_groups = [sg.name for sg in server.list_security_group()]
            # The port handler has to handle all network/port related changes

        return current

    @cache(timeout=10)
    def _port_id(self, port_name):
        ports = self._neutron.list_ports(name=port_name)
        if len(ports["ports"]) > 0:
            return ports["ports"][0]["id"]

        return None

    @cache(timeout=10)
    def _get_subnet_id(self, subnet_name):
        subnets = self._neutron.list_subnets(name=subnet_name)
        if len(subnets["subnets"]) > 0:
            return subnets["subnets"][0]["network_id"]

        return None

    @cache(timeout=60)
    def get_security_group(self, name=None, id=None):
        """
            Get security group details from openstack
        """
        if name is not None:
            sgs = self._neutron.list_security_groups(name=name)
        elif id is not None:
            sgs = self._neutron.list_security_groups(id=id)

        if len(sgs["security_groups"]) == 0:
            return None

        return sgs["security_groups"][0]

    def _create_nic_config(self, port):
        nic = {}
        port_id = self._port_id(port["name"])
        if port_id is None:
            network = self._get_subnet_id(port["network"])
            if network is None:
                return None
            nic["net-id"] = network
            if not port["dhcp"] and port["address"] is not None:
                nic["v4-fixed-ip"] = port["address"]
        else:
            nic["port-id"] = port_id

        return nic

    def _build_nic_list(self, ports):
        # build a list of nics for this server based on the index in the ports
        no_sort = sorted([p for p in ports if p["index"] == 0], key=lambda x: x["network"])
        sort = sorted([p for p in ports if p["index"] > 0], key=lambda x: x["index"])

        return [self._create_nic_config(p) for p in sort] + [self._create_nic_config(p) for p in no_sort]

    def _build_sg_list(self, security_groups):
        sg_list = []
        for group in security_groups:
            sg = self.get_security_group(name=group)
            if sg is not None:
                sg_list.append(sg["name"])
        return sg_list

    def do_changes(self, ctx, resource, changes):
        # First ensure the key is there.
        # TODO: move this to a specific resource
        keys = {k.name: k for k in self._client.keypairs.list()}
        if resource.key_name not in keys:
            self._client.keypairs.create(resource.key_name, resource.key_value)

        if "purged" in changes:
            if changes["purged"][0]:  # create
                flavor = self._client.flavors.find(name=resource.flavor)
                nics = self._build_nic_list(resource.ports)
                server = self._client.servers.create(resource.name, flavor=flavor.id,
                                                     security_groups=self._build_sg_list(resource.security_groups),
                                                     image=resource.image, key_name=resource.key_name,
                                                     userdata=resource.user_data, nics=nics)
            elif changes["purged"][1]:
                server = self._client.servers.find(name=resource.name)
                server.delete()

        elif "security_groups" in changes:
            current = set(changes["security_groups"][0])
            desired = set(changes["security_groups"][1])

            server = self._client.servers.find(name=resource.name)
            for new_rule in (desired - current):
                self._client.servers.add_security_group(server, new_rule)

            for remove_rule in (current - desired):
                self._client.servers.remove_security_group(server, remove_rule)

        return True

    def facts(self, ctx, resource):
        LOGGER.debug("Finding facts for %s" % resource.id.resource_str())

        try:
            vm = self.cache.get_or_else(key="nova_servers", function=self._client.servers.find, timeout=60, name=resource.name)

            networks = vm.networks

            facts = {}
            for name, ips in networks.items():
                for i in range(len(ips)):
                    facts["subnet_%s_ip_%d" % (name, i)] = ips[i]
                    if i == 0:
                        facts["subnet_%s_ip" % name] = ips[i]

            # report the first ip of the port with index 1 as "ip_address"
            for port in resource.ports:
                if port["index"] == 1:
                    if port["network"] in networks:
                        ips = networks[port["network"]]
                        facts["ip_address"] = ips[0]

            return facts
        except Exception:
            return {}


class NeutronHandler(ResourceHandler):
    """
        Holds common routines for all neutron handlers
    """
    __connections = {}

    @classmethod
    def is_available(cls, io):
        return True

    def _diff(self, current, desired):
        changes = {}

        # check attributes
        for field in desired.__class__.fields:
            current_value = getattr(current, field)
            desired_value = getattr(desired, field)

            if desired_value is not None and current_value is None:
                try:
                    id = getattr(current, field + "_id")
                    converter = getattr(self, "get_%s_id" % field)
                    desired_value = converter(self.get_project_id(desired, desired.project), desired_value)
                    current_value = id
                except AttributeError:
                    pass

            if current_value != desired_value:
                changes[field] = (current_value, desired_value)

        return changes

    def __init__(self, agent, io=None):
        super().__init__(agent, io)

        self._client = None
        self._session = None

    def pre(self, ctx, resource):
        """
            Setup a connection with neutron
        """
        key = (resource.auth_url, resource.admin_user, resource.admin_password, resource.admin_tenant)
        if key in NeutronHandler.__connections:
            self._client, self._session = NeutronHandler.__connections[key]
        else:
            auth = v2.Password(auth_url=resource.auth_url, username=resource.admin_user,
                               password=resource.admin_password, tenant_name=resource.admin_tenant)
            self._session = session.Session(auth=auth)
            self._client = neutron_client.Client("2.0", session=self._session)
            NeutronHandler.__connections[key] = (self._client, self._session)

    @cache(ignore=["resource"])
    def get_project_id(self, resource, name):
        """
            Retrieve the id of a project based on the given name
        """
        kc = keystone_client.Client(session=self._session)

        # Fallback for non admin users
        if resource.admin_tenant == name:
            return self._session.get_project_id()

        try:
            tenant = kc.tenants.find(name=name)
            return tenant.id
        except Exception:
            return None

    def get_network_id(self, project_id, name):
        """
            Retrieve the network id based on the name of the network
        """
        if project_id is not None:
            networks = self._client.list_networks(tenant_id=project_id, name=name)
        else:
            networks = self._client.list_networks(name=name)

        if len(networks["networks"]) == 0:
            return None

        elif len(networks["networks"]) > 1:
            raise Exception("Found more than one network with name %s for project %s" % (name, project_id))

        else:
            return networks["networks"][0]["id"]

    def get_subnet_id(self, project_id, name):
        """
            Retrieve the subnet id based on the name of the network
        """
        subnets = self._client.list_subnets(tenant_id=project_id, name=name)

        if len(subnets["subnets"]) == 0:
            return None

        elif len(subnets["subnets"]) > 1:
            raise Exception("Found more than one subnet with name %s for project %s" % (name, project_id))

        else:
            return subnets["subnets"][0]["id"]

    def get_router_id(self, project_id, name):
        """
            Retrieve the router id based on the name of the network
        """
        routers = self._client.list_routers(name=name)

        if len(routers["routers"]) == 0:
            return None

        elif len(routers["routers"]) > 1:
            raise Exception("Found more than one router with name %s for project %s" % (name, project_id))

        else:
            return routers["routers"][0]["id"]

    def get_host_id(self, project_id, name):
        return self.get_host(project_id, name).id

    def get_host(self, project_id, name):
        """
            Retrieve the router id based on the name of the network
        """
        nc = nova_client.Client("2", session=self._session)
        vms = nc.servers.findall(name=name)

        if len(vms) == 0:
            return None

        elif len(vms) > 1:
            raise Exception("Found more than one VM with name %s for project %s" % (name, project_id))

        else:
            return vms[0]

    def get_host_for_id(self, id):
        """
            Retrieve the router id based on the name of the network
        """
        nc = nova_client.Client("2", session=self._session)
        vms = nc.servers.findall(id=id)

        if len(vms) == 0:
            return None

        elif len(vms) > 1:
            raise Exception("Found more than one VM with id %s" % (id))

        else:
            return vms[0]

    def post(self, ctx, resource):
        self._client = None


@provider("openstack::Network", name="openstack")
class NetworkHandler(OpenStackHandler):
    def read_resource(self, ctx: handler.HandlerContext, resource: resources.PurgeableResource):
        network = self.facts(ctx, resource)

        if len(network) > 0:
            resource.purged = False
            resource.external = network["router:external"]
            if resource.physical_network != "":
                resource.physical_network = network["provider:physical_network"]

            if resource.network_type != "":
                resource.network_type = network["provider:network_type"]

            if resource.segmentation_id > 0:
                resource.segmentation_id = network["provider:segmentation_id"]

            ctx.set("network_id", network["id"])
            ctx.set("project_id", network["tenant_id"])

        else:
            raise ResourcePurged()

    def _create_dict(self, resource: Network, project_id):
        net = {"name": resource.name, "tenant_id": project_id, "admin_state_up": True}

        if resource.physical_network != "":
            net["provider:physical_network"] = resource.physical_network

        if resource.network_type != "":
            net["provider:network_type"] = resource.network_type

        if resource.segmentation_id > 0:
            net["provider:segmentation_id"] = resource.segmentation_id

        return net

    def create_resource(self, ctx: handler.HandlerContext, resource: resources.PurgeableResource):
        project_id = self.get_project_id(resource, resource.project)
        self._neutron.create_network({"network": self._create_dict(resource, project_id)})
        ctx.set_created()

    def delete_resource(self, ctx: handler.HandlerContext, resource: resources.PurgeableResource):
        network_id = ctx.get("network_id")
        self._neutron.delete_network(network_id)
        ctx.set_purged()

    def update_resource(self, ctx: handler.HandlerContext, changes: dict, resource: resources.PurgeableResource):
        network_id = ctx.get("network_id")
        self._neutron.update_network(network_id, {"network": {"name": resource.name, "router:external": resource.external}})

        ctx.fields_updated(("name", "external"))
        ctx.set_updated()

    def facts(self, ctx, resource: Network):
        try:
            networks = self._neutron.list_networks(name=resource.name)["networks"]
        except NotFound:
            return {}

        if len(networks) == 0:
            return {}

        if len(networks) > 1:
            LOGGER.warning("Multiple networks with the same name available!")
            return {}

        return networks[0]


@provider("openstack::Router", name="openstack")
class RouterHandler(NeutronHandler):
    def check_resource(self, ctx, resource):
        current = resource.clone()
        neutron_version = self.facts(resource)

        if len(neutron_version) > 0:
            current.id = neutron_version["id"]
        else:
            current.id = NULL_UUID
            current.purged = True

        # get a list of all attached subnets
        if current.id != NULL_UUID:
            ext_name = ""
            external_net_id = ""
            if "external_gateway_info" in neutron_version and \
                    neutron_version["external_gateway_info"] is not None:
                external_net_id = neutron_version["external_gateway_info"]["network_id"]

                networks = self._client.list_networks(id=external_net_id)
                if len(networks["networks"]) == 1:
                    ext_name = networks["networks"][0]["name"]

            current.gateway = ext_name

            ports = self._client.list_ports(device_id=current.id)
            subnet_list = []
            for port in ports["ports"]:
                subnets = port["fixed_ips"]
                if port["name"] == "" or port["name"] not in current.ports:
                    for subnet in subnets:
                        if subnet != NULL_UUID:
                            try:
                                subnet_details = self._client.show_subnet(subnet["subnet_id"])
                                if subnet_details["subnet"]["network_id"] != external_net_id:
                                    subnet_list.append(subnet_details["subnet"]["name"])

                            except exceptions.NeutronClientException:
                                pass

            current.subnets = sorted(subnet_list)

            routes = {}
            for route in neutron_version["routes"]:
                routes[route["destination"]] = route["nexthop"]

            current.routes = routes

        else:
            current.gateway = ""
            current.subnets = []
            current.routes = {}

        return current

    def do_changes(self, ctx, resource: Router, changes):
        changed = False
        deleted = False

        project_id = self.get_project_id(resource, resource.project)
        if project_id is None:
            raise SkipResource("Cannot create network when project id is not yet known.")

        if "purged" in changes:
            if not changes["purged"][1]:  # create the network
                self._client.create_router({"router": {"name": resource.name, "tenant_id": project_id}})
                changed = True

            elif changes["purged"][1] and not changes["purged"][0]:
                self._client.delete_router(resource.id)
                changed = True
                deleted = True

        elif "name" in changes:
            self._client.update_router(resource.id, {"router": {"name": resource.name}})
            changed = True

        if deleted:
            return changed

        router_facts = self.facts(resource)

        # if the router exists and changes are required in the interfaces, make them
        if "subnets" in changes:
            current = set(changes["subnets"][0])
            to = set(changes["subnets"][1])

            # subnets to add to the router
            for subnet in (to - current):
                # query for the subnet id
                subnet_data = self._client.list_subnets(name=subnet)
                if "subnets" not in subnet_data or len(subnet_data["subnets"]) != 1:
                    raise Exception("Unable to find id of subnet %s" % subnet)

                subnet_id = subnet_data["subnets"][0]["id"]

                self._client.add_interface_router(router=router_facts["id"], body={"subnet_id": subnet_id})

            # subnets to delete
            for subnet in (current - to):
                # query for the subnet id
                subnet_data = self._client.list_subnets(name=subnet)
                if "subnets" not in subnet_data or len(subnet_data["subnets"]) != 1:
                    raise Exception("Unable to find id of subnet %s" % subnet)

                subnet_id = subnet_data["subnets"][0]["id"]

                self._client.remove_interface_router(router=router_facts["id"], body={"subnet_id": subnet_id})

        if "gateway" in changes:
            network_id = self.get_network_id(None, changes["gateway"][1])
            if network_id is None:
                raise Exception("Unable to set router gateway because the gateway network that does not exist.")

            self._client.add_gateway_router(router_facts["id"], {'network_id': network_id})

        if "routes" in changes:
            self._client.update_router(router_facts["id"], {"router": {"routes": [{"nexthop": n, "destination": d}
                                                                                  for d, n in resource.routes.items()]}})

        return changed

    def facts(self, ctx, resource: Router):
        """
            Get facts about this resource
        """
        routers = self._client.list_routers(name=resource.name)

        if "routers" not in routers:
            return {}

        filtered_list = [rt for rt in routers["routers"] if rt["name"] == resource.name]

        if len(filtered_list) == 0:
            return {}

        if len(filtered_list) > 1:
            LOGGER.warning("Multiple routers with the same name available!")
            return {}

        router = filtered_list[0]
        return router


@provider("openstack::Subnet", name="openstack")
class SubnetHandler(NeutronHandler):
    def check_resource(self, ctx, resource):
        """
            Check the state of the resource
        """
        current = resource.clone()
        neutron_version = self.facts(resource)

        if len(neutron_version) > 0:
            current.id = neutron_version["id"]
            current.network_address = neutron_version["cidr"]
            current.dhcp = neutron_version["enable_dhcp"]
            current.network_id = neutron_version["network_id"]

            pool = neutron_version["allocation_pools"][0]
            if resource.allocation_start != "" and resource.allocation_end != "":  # only change when they are both set
                current.allocation_start = pool["start"]
                current.allocation_end = pool["end"]

        else:
            current.id = 0
            current.purged = True
            current.network_address = ""
            current.network_id = NULL_UUID
            current.allocation_start = ""
            current.allocation_end = ""

        return current

    def do_changes(self, ctx, resource: Subnet, changes):
        project_id = self.get_project_id(resource, resource.project)
        if project_id is None:
            raise SkipResource("Cannot create network when project id is not yet known.")

        network_id = self.get_network_id(project_id, resource.network)
        if network_id is None:
            raise Exception("Unable to create subnet because of network that does not exist.")

        changed = False
        if "purged" in changes:
            if not changes["purged"][1]:  # create the network
                body = {"name": resource.name,
                        "network_id": network_id,
                        "enable_dhcp": resource.dhcp,
                        "cidr": resource.network_address,
                        "ip_version": 4,
                        "tenant_id": project_id}

                if len(resource.allocation_start) > 0 and len(resource.allocation_end) > 0:
                    body["allocation_pools"] = [{"start": resource.allocation_start,
                                                 "end": resource.allocation_end}]

                self._client.create_subnet({"subnet": body})
                changed = True

            elif changes["purged"][1] and not changes["purged"][0]:
                self._client.delete_subnet(resource.id)
                changed = True

        elif len(changes) > 0:
            neutron_version = self.facts(resource)
            body = {"subnet": {"enable_dhcp": resource.dhcp}}
            if len(resource.allocation_start) > 0 and len(resource.allocation_end) > 0:
                body["allocation_pools"] = [{"start": resource.allocation_start,
                                             "end": resource.allocation_end}]

            self._client.update_subnet(neutron_version["id"], body)
            changed = True

        return changed

    @cache(timeout=5)
    def facts(self, ctx, resource):
        subnets = self._client.list_subnets(name=resource.name)

        if "subnets" not in subnets:
            return {}

        filtered_list = [sn for sn in subnets["subnets"] if sn["name"] == resource.name]

        if len(filtered_list) == 0:
            return {}

        if len(filtered_list) > 1:
            LOGGER.warning("Multiple subnets with the same name available!")
            return {}

        subnet = filtered_list[0]
        return subnet


@provider("openstack::RouterPort", name="openstack")
class RouterPortHandler(NeutronHandler):
    def check_resource(self, ctx, resource: RouterPort) -> RouterPort:
        current = resource.clone()
        neutron_version = self.facts(resource)

        if len(neutron_version) > 0:
            current.id = neutron_version["id"]
            if neutron_version["device_id"] == "":
                current.router_id = NULL_ID
            else:
                current.router_id = neutron_version["device_id"]

            current.network_id = neutron_version["network_id"]

        else:
            current.id = 0
            current.purged = True
            current.subnet_id = NULL_UUID
            current.router_id = NULL_UUID
            current.network_id = NULL_UUID
            current.address = ""

        return current

    def do_changes(self, ctx, resource: RouterPort, changes):
        project_id = self.get_project_id(resource, resource.project)
        if project_id is None:
            raise SkipResource("Cannot create network when project id is not yet known.")

        subnet_id = self.get_subnet_id(project_id, resource.subnet)
        if subnet_id is None:
            raise SkipResource("Unable to create router port because the subnet does not exist.")

        network_id = self.get_network_id(project_id, resource.network)
        if network_id is None:
            raise SkipResource("Unable to create router port because the network does not exist.")

        router_id = self.get_router_id(project_id, resource.router)
        if router_id is None:
            raise SkipResource("Unable to create router port because the router does not exist.")

        changed = False
        if "purged" in changes:
            if not changes["purged"][1]:  # create the router port
                body_value = {'port': {
                    'admin_state_up': True,
                    'name': resource.name,
                    'network_id': network_id
                }
                }
                if resource.address != "":
                    body_value["port"]["fixed_ips"] = [{"subnet_id": subnet_id, "ip_address": resource.address}]

                result = self._client.create_port(body=body_value)

                if "port" not in result:
                    raise Exception("Unable to create port.")

                port_id = result["port"]["id"]

                # attach it to the router
                self._client.add_interface_router(router_id, body={"port_id": port_id})
                changed = True

            elif changes["purged"][1] and not changes["purged"][0]:
                # self._client.delete_port(resource.id)
                changed = True

        elif len(changes) > 0:
            # TODO

            changed = True

        return changed

    def facts(self, ctx, resource: RouterPort):
        ports = self._client.list_ports(name=resource.name)

        if "ports" not in ports:
            return {}

        filtered_list = [port for port in ports["ports"] if port["name"] == resource.name]

        if len(filtered_list) == 0:
            return {}

        if len(filtered_list) > 1:
            LOGGER.warning("Multiple ports with the same name available!")
            return {}

        port = filtered_list[0]
        return port


@provider("openstack::HostPort", name="openstack")
class HostPortHandler(NeutronHandler):
    def get_port(self, network_id, device_id):
        ports = self._client.list_ports(network_id=network_id, device_id=device_id)["ports"]
        if len(ports) > 0:
            return ports[0]
        return None

    def check_resource(self, ctx, resource: HostPort) -> HostPort:
        current = resource.clone()

        project_id = self.get_project_id(resource, resource.project)
        network_id = self.get_network_id(project_id, resource.network)
        vm = self.get_host(project_id, resource.host)
        if vm is not None:
            port = self.get_port(network_id, vm.id)
        else:
            port = None

        if port is not None:
            # we always use port 0 as the managed port in case multiple ports exist on this network
            # for this given VM
            if not resource.dhcp:
                current.address = port["fixed_ips"][0]["ip_address"]
            current.portsecurity = port["port_security_enabled"]
            current.name = port["name"]

        else:
            current.id = 0
            current.purged = True
            current.address = ""

        return current

    def do_changes(self, ctx, resource: HostPort, changes):
        project_id = self.get_project_id(resource, resource.project)
        if project_id is None:
            raise SkipResource("Cannot create network when project id is not yet known.")

        subnet_id = self.get_subnet_id(project_id, resource.subnet)
        if subnet_id is None:
            raise SkipResource("Unable to create host port because the subnet does not exist.")

        network_id = self.get_network_id(project_id, resource.network)
        if network_id is None:
            raise SkipResource("Unable to create host port because the network does not exist.")

        vm = self.get_host(project_id, resource.host)
        if vm is None:
            raise SkipResource("Unable to create host port because the router does not exist.")

        port = self.get_port(network_id, vm.id)

        try:
            changed = False
            if "purged" in changes and port is None:
                if not changes["purged"][1]:  # create the router port
                    body_value = {'port': {'admin_state_up': True, 'name': resource.name, 'network_id': network_id}}

                    if resource.address != "" and not resource.dhcp:
                        body_value["port"]["fixed_ips"] = [{"subnet_id": subnet_id, "ip_address": resource.address}]

                    result = self._client.create_port(body=body_value)

                    if "port" not in result:
                        raise Exception("Unable to create port.")

                    port_id = result["port"]["id"]

                    # attach it to the host
                    vm.interface_attach(port_id, None, None)
                    changed = True

                elif changes["purged"][1] and not changes["purged"][0]:
                    self._client.delete_port(port["id"])
                    changed = True
            else:
                port = self.get_port(network_id, vm.id)
                if port is None:
                    raise SkipResource("Port not found")

                if "portsecurity" in changes and not changes["portsecurity"][1]:
                    self._client.update_port(port=port["id"], body={"port": {"port_security_enabled": False,
                                                                    "security_groups": None}})

                    del changes["portsecurity"]
                    changed = True

                if "name" in changes:
                    self._client.update_port(port=port["id"], body={"port": {"name": resource.name}})
                    del changes["name"]

                if len(changes) > 0:
                    # TODO
                    raise SkipResource("not implemented, %s" % changes)
                    changed = True
        except novaclient.exceptions.Conflict as e:
            raise SkipResource("Host is not ready: %s" % str(e))

        return changed

    @cache(timeout=5)
    def facts(self, ctx, resource):
        """
            Get facts about this resource
        """
        ports = self._client.list_ports(name=resource.name)

        if "ports" not in ports:
            return {}

        filtered_list = [port for port in ports["ports"] if port["name"] == resource.name]

        if len(filtered_list) == 0:
            return {}

        if len(filtered_list) > 1:
            LOGGER.warning("Multiple ports with the same name available!")
            return {}

        port = filtered_list[0]
        return port


@provider("openstack::SecurityGroup", name="openstack")
class SecurityGroupHandler(NeutronHandler):
    @cache(timeout=60)
    def get_security_group(self, name=None, id=None):
        """
            Get security group details from openstack
        """
        if name is not None:
            sgs = self._client.list_security_groups(name=name)
        elif id is not None:
            sgs = self._client.list_security_groups(id=id)

        if len(sgs["security_groups"]) == 0:
            return None

        return sgs["security_groups"][0]

    def check_resource(self, ctx, resource: SecurityGroup) -> SecurityGroup:
        current = resource.clone()
        sg = self.get_security_group(name=resource.name)
        if sg is None:
            current.purged = True
            current.rules = []
            return current

        current.description = sg["description"]
        current.rules = []
        for rule in sg["security_group_rules"]:
            if rule["ethertype"] != "IPv4":
                continue

            current_rule = {"__id": rule["id"]}
            if rule["protocol"] is None:
                current_rule["protocol"] = "all"
            else:
                current_rule["protocol"] = rule["protocol"]

            if rule["remote_ip_prefix"] is not None:
                current_rule["remote_ip_prefix"] = rule["remote_ip_prefix"]

            elif rule["remote_group_id"] is not None:
                rgi = self.get_security_group(id=rule["remote_group_id"])
                current_rule["remote_group"] = rgi["name"]

            else:
                current_rule["remote_ip_prefix"] = "0.0.0.0/0"

            current_rule["direction"] = rule["direction"]
            current_rule["port_range_min"] = rule["port_range_min"]
            current_rule["port_range_max"] = rule["port_range_max"]

            current.rules.append(current_rule)

        return current

    def _compare_rule(self, old, new):
        old_keys = set([x for x in old.keys() if not x.startswith("__")])
        new_keys = set([x for x in new.keys() if not x.startswith("__")])

        if old_keys != new_keys:
            return False

        for key in old_keys:
            if old[key] != new[key]:
                return False

        return True

    def _update_rules(self, group_id, resource, changes):
        # # Update rules. First add all new rules, than remove unused rules
        old_rules = list(changes["rules"][0])
        new_rules = list(changes["rules"][1])

        for new_rule in changes["rules"][1]:
            for old_rule in changes["rules"][0]:
                if self._compare_rule(old_rule, new_rule):
                    old_rules.remove(old_rule)
                    new_rules.remove(new_rule)
                    break

        for new_rule in new_rules:
            new_rule["ethertype"] = "IPv4"
            if "remote_group" in new_rule:
                if new_rule["remote_group"] is not None:
                    # lookup the id of the group
                    groups = self._client.list_security_groups(name=new_rule["remote_group"])["security_groups"]
                    if len(groups) == 0:
                        # TODO: log skip rule
                        continue  # Do not update this rule

                    del new_rule["remote_group"]
                    new_rule["remote_group_id"] = groups[0]["id"]

                else:
                    del new_rule["remote_group_id"]

            new_rule["security_group_id"] = group_id

            if new_rule["protocol"] == "all":
                new_rule["protocol"] = None

            try:
                self._client.create_security_group_rule({'security_group_rule': new_rule})
            except exceptions.Conflict:
                LOGGER.exception("Rule conflict for rule %s", new_rule)
                raise

        for old_rule in old_rules:
            try:
                self._client.delete_security_group_rule(old_rule["__id"])
            except exceptions.NotFound:
                # TODO: handle this
                pass

    def list_changes(self, ctx, resource):
        """
            List the changes that are required to the security group
        """
        current = self.check_resource(ctx, resource)
        changes = self._diff(current, resource)

        if "rules" in changes:
            old_rules = list(changes["rules"][0])
            new_rules = list(changes["rules"][1])

            for new_rule in changes["rules"][1]:
                for old_rule in changes["rules"][0]:
                    if self._compare_rule(old_rule, new_rule):
                        old_rules.remove(old_rule)
                        new_rules.remove(new_rule)
                        break

            if len(old_rules) == 0 and len(new_rules) == 0:
                del changes["rules"]

        return changes

    def do_changes(self, ctx, resource: SecurityGroup, changes) -> SecurityGroup:
        """
            Enforce the changes
        """
        sg_id = None
        if "purged" in changes:
            changed = True
            if changes["purged"][0] == True:  # create
                sg = self._client.create_security_group({"security_group": {"name": resource.name,
                                                                            "description": resource.description}})
                sg_id = sg["security_group"]["id"]
            else:  # purge
                sg = self.get_security_group(name=resource.name)
                if sg is not None:
                    self._client.delete_security_group(sg["id"])
                    sg_id = sg["id"]

        elif len(changes) > 0:
            sg = self.get_security_group(name=resource.name)
            if sg is None:
                raise Exception("Unable to modify unexisting security group")

            self._client.update_security_group(sg["id"], {"security_group": {"name": resource.name,
                                                                             "description": resource.description}})
            sg_id = sg["id"]

        if "rules" in changes:
            self._update_rules(sg_id, resource, changes)

        return changed

    @cache(timeout=5)
    def facts(self, ctx, resource):
        """
            Discover facts about this securitygroup
        """
        return {}


@provider("openstack::FloatingIP", name="openstack")
class FloatingIPHandler(NeutronHandler):
    @cache(timeout=10)
    def get_port_id(self, name):
        ports = self._client.list_ports(name=name)["ports"]
        if len(ports) == 0:
            return None

        elif len(ports) == 1:
            return ports[0]["id"]
        else:
            raise Exception("Multiple ports found with name %s" % name)

    @cache(timeout=10)
    def get_floating_ip(self, port_id):
        fip = self._client.list_floatingips(port_id=port_id)["floatingips"]
        if len(fip) == 0:
            return None

        else:
            return fip[0]["id"]

    def check_resource(self, ctx, resource: FloatingIP) -> FloatingIP:
        current = resource.clone()
        port_id = self.get_port_id(resource.port)
        fip = self.get_floating_ip(port_id)
        if fip is None:
            current.purged = True

        else:
            current.purged = False

        return current

    def _find_available_fips(self, project_id, network_id):
        available_fips = []
        floating_ips = self._client.list_floatingips(floating_network_id=network_id, tenant_id=project_id)["floatingips"]
        for fip in floating_ips:
            if fip["port_id"] is None:
                available_fips.append(fip)

        return available_fips

    def do_changes(self, ctx, resource: FloatingIP, changes) -> FloatingIP:
        changed = False

        project_id = self.get_project_id(resource, resource.project)
        network_id = self.get_network_id(None, resource.external_network)
        port_id = self.get_port_id(resource.port)

        if "purged" in changes:
            if changes["purged"][0]:  # create
                available_fips = self._find_available_fips(project_id, network_id)
                if len(available_fips) > 0:
                    fip_id = available_fips[0]["id"]
                    self._client.update_floatingip(fip_id, {"floatingip": {"port_id": port_id, "description": resource.name}})

                else:
                    self._client.create_floatingip({"floatingip": {"port_id": port_id, "floating_network_id": network_id,
                                                                   "description": resource.name}})

                changed = True

            else:
                # disassociate and purge
                fip_id = self.get_floating_ip(port_id)
                if fip_id is not None:
                    self._client.delete_floatingip(fip_id)

        return changed

    @cache(timeout=5)
    def facts(self, ctx, resource):
        port_id = self.get_port_id(resource.port)
        fip = self._client.list_floatingips(port_id=port_id)["floatingips"]
        if len(fip) == 0:
            return {}

        else:
            return {"ip_address": fip[0]["floating_ip_address"]}


@dependency_manager
def keystone_dependencies(config_model, resource_model):
    projects = {}
    users = {}
    roles = []
    for _, res in resource_model.items():
        if res.id.entity_type == "openstack::Project":
            projects[res.name] = res

        elif res.id.entity_type == "openstack::User":
            users[res.name] = res

        elif res.id.entity_type == "openstack::Role":
            roles.append(res)

    for role in roles:
        if role.project not in projects:
            raise Exception("The project %s of role %s is not defined in the model." % (role.project, role.role_id))

        if role.user not in users:
            raise Exception("The user %s of role %s is not defined in the model." % (role.user, role.role_id))

        role.requires.add(projects[role.project])
        role.requires.add(users[role.user])


@provider("openstack::Project", name="openstack")
class ProjectHandler(OpenStackHandler):
    def read_resource(self, ctx, resource):
        try:
            project = self._keystone.projects.find(name=resource.name)
            resource.purged = False
            resource.enabled = project.enabled
            resource.description = project.description
            ctx.set("project", project)
        except NotFound:
            raise ResourcePurged()

    def create_resource(self, ctx, resource: resources.PurgeableResource) -> None:
        self._keystone.projects.create(resource.name, description=resource.description, enabled=resource.enabled,
                                       domain="default")
        ctx.set_created()

    def delete_resource(self, ctx, resource: resources.PurgeableResource) -> None:
        ctx.get("project").delete()
        ctx.set_purged()

    def update_resource(self, ctx, changes: dict, resource: resources.PurgeableResource) -> None:
        ctx.get("project").update(name=resource.name, description=resource.description, enabled=resource.enabled)
        ctx.set_updated()

    def facts(self, ctx, resource: Project):
        keystone = self.get_connection(resource)
        try:
            project = keystone.tenants.find(name=resource.name)
            return {"id": project.id, "name": project.name}
        except Exception:
            return {}


@provider("openstack::User", name="openstack")
class UserHandler(OpenStackHandler):
    def read_resource(self, ctx, resource):
        try:
            user = self._keystone.users.find(name=resource.name)
            resource.purged = False
            resource.enabled = user.enabled
            resource.email = user.email
            ctx.set("user", user)

            # if a password is provided (not ""), check if it works otherwise mark it as "***"
            if resource.password != "":
                try:
                    s = keystone_client.Client(auth_url=resource.auth_url, username=resource.name, password=resource.password)
                    s.authenticate()
                except Exception:
                    resource.password = "***"

        except NotFound:
            raise ResourcePurged()

    def create_resource(self, ctx, resource: resources.PurgeableResource) -> None:
        self._keystone.users.create(resource.name, password=resource.password, email=resource.email, enabled=resource.enabled)
        ctx.set_created()

    def delete_resource(self, ctx, resource: resources.PurgeableResource) -> None:
        ctx.get("user").delete()
        ctx.set_purged()

    def update_resource(self, ctx, changes: dict, resource: resources.PurgeableResource) -> None:
        user_id = ctx.get("user").id
        if resource.password != "":
            self._keystone.users.update(user_id, password=resource.password, email=resource.email, enabled=resource.enabled)
        else:
            self._keystone.users.update(user_id, email=resource.email, enabled=resource.enabled)
        ctx.set_updated()


@provider("openstack::Role", name="openstack")
class RoleHandler(OpenStackHandler):
    """ creates roles and user, project, role assocations """
    def read_resource(self, ctx, resource):
        # get the role
        role = None
        try:
            self._keystone.users.find(name="bartvb")
            role = self._keystone.roles.find(name=resource.role)
        except NotFound:
            pass

        try:
            user = self._keystone.users.find(name=resource.user)
            project = self._keystone.projects.find(name=resource.project)
        except NotFound:
            raise SkipResource("Either the user or project does not exist.")

        try:
            self._keystone.roles.check(role=role, user=user, project=project)
            resource.purged = False
        except Exception:
            resource.purged = True

        ctx.set("role", role)
        ctx.set("user", user)
        ctx.set("project", project)

    def create_resource(self, ctx, resource: resources.PurgeableResource) -> None:
        user = ctx.get("user")
        project = ctx.get("project")
        role = ctx.get("role")

        if role is None:
            role = self._keystone.roles.create(resource.role)

        self._keystone.roles.grant(user=user, role=role, project=project)
        ctx.set_created()

    def delete_resource(self, ctx, resource: resources.PurgeableResource) -> None:
        user = ctx.get("user")
        project = ctx.get("project")
        role = ctx.get("role")

        self._keystone.roles.revoke(user=user, role=role, project=project)
        ctx.set_purged()

    def update_resource(self, ctx, changes: dict, resource: resources.PurgeableResource) -> None:
        assert False, "This should not happen"


@provider("openstack::Service", name="openstack")
class ServiceHandler(OpenStackHandler):
    def read_resource(self, ctx, resource):
        service = None
        try:
            service = self._keystone.services.find(name=resource.name, type=resource.type)
            resource.description = service.description
            resource.purged = False
        except NotFound:
            resource.purged = True
            resource.description = None
            resource.name = None
            resource.type = None

        ctx.set("service", service)

    def create_resource(self, ctx, resource: resources.PurgeableResource) -> None:
        self._keystone.services.create(resource.name, resource.type, description=resource.description)
        ctx.set_created()

    def delete_resource(self, ctx, resource: resources.PurgeableResource) -> None:
        ctx.get("service").delete()
        ctx.set_purged()

    def update_resource(self, ctx, changes: dict, resource: resources.PurgeableResource) -> None:
        self._keystone.services.update(ctx.get("service"), description=resource.description)
        ctx.set_updated()


@provider("openstack::EndPoint", name="openstack")
class EndpointHandler(OpenStackHandler):
    types = {"admin": "admin_url", "internal": "internal_url", "public": "public_url"}
    def read_resource(self, ctx, resource):
        service = None
        for s in self._keystone.services.list():
            if resource.service_id == "%s_%s" % (s.type, s.name):
                service = s

        if service is None:
            raise SkipResource("Unable to find service to which endpoint belongs")

        endpoints = {}
        try:
            endpoints = {e.interface: e for e in self._keystone.endpoints.list(region=resource.region, service=service)}
            for k, v in EndpointHandler.types.items():
                setattr(resource, v, endpoints[k] if k in endpoints else None)

            resource.purged = False
        except NotFound:
            resource.purged = True
            resource.region = None
            resource.internal_url = None
            resource.admin_url = None
            resource.public_url = None

        ctx.set("service", service)
        ctx.set("endpoints", endpoints)

    def create_resource(self, ctx, resource: resources.PurgeableResource) -> None:
        assert False, "Should never get here"

    def delete_resource(self, ctx, resource: resources.PurgeableResource) -> None:
        for endpoint in ctx.get("endpoints"):
            endpoint.delete()

        ctx.set_purged()

    def update_resource(self, ctx, changes: dict, resource: resources.PurgeableResource) -> None:
        service = ctx.get("service")
        endpoints = ctx.get("endpoints")

        for k, v in EndpointHandler.types.items():
            if k not in endpoints:
                self._keystone.endpoints.create(service, url=getattr(resource, v), region=resource.region, interface=k)
                ctx.set_created()

            elif v in changes:
                self._keystone.endpoints.update(endpoints[k], url=getattr(resource, v))
                ctx.set_updated()