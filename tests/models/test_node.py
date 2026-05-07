"""Tests for the Node base model."""


from satgonetem.models.node import Node
from satgonetem.models.interface import Interface


class TestNodeInit:
    """Tests for Node.__init__."""

    def test_name_stored(self):
        node = Node("Sat5")
        assert node.name == "Sat5"

    def test_id_parsed_from_name(self):
        assert Node("Sat5").id == 5
        assert Node("Sat0").id == 0
        assert Node("Sat12").id == 12

    def test_routing_tables_start_empty(self):
        node = Node("Sat0")
        assert node.ipv4_routing_table == []

    def test_interfaces_start_empty(self):
        node = Node("Sat0")
        assert node.interfaces == []

    def test_position_starts_empty(self):
        node = Node("Sat0")
        assert node.position == {}

    def test_command_output_starts_empty(self):
        node = Node("Sat0")
        assert node.command_output == ""

    def test_loopback_interface_created(self):
        node = Node("Sat0")
        assert node.loopback is not None
        assert node.loopback.name == "lo"
        assert node.loopback.type == "lo"


class TestNodeCreateInterface:
    """Tests for Node.create_interface."""

    def test_returns_interface_with_correct_name(self):
        node = Node("Sat0")
        iface = node.create_interface("Sat0.1")
        assert isinstance(iface, Interface)
        assert iface.name == "Sat0.1"

    def test_interface_appended_to_list(self):
        node = Node("Sat0")
        iface = node.create_interface("Sat0.1")
        assert iface in node.interfaces
        assert len(node.interfaces) == 1

    def test_multiple_interfaces(self):
        node = Node("Sat0")
        node.create_interface("Sat0.1")
        node.create_interface("Sat0.2")
        assert len(node.interfaces) == 2


class TestNodeGetInterfaces:
    """Tests for Node.get_interfaces."""

    def test_returns_empty_list_initially(self):
        node = Node("Sat0")
        assert node.get_interfaces() == []

    def test_returns_created_interfaces(self):
        node = Node("Sat0")
        iface = node.create_interface("Sat0.1")
        result = node.get_interfaces()
        assert result == [iface]

    def test_returns_same_object_as_interfaces_attr(self):
        node = Node("Sat0")
        assert node.get_interfaces() is node.interfaces


class TestNodeEquality:
    """Tests for Node.__eq__ and __hash__."""

    def test_equal_nodes_with_same_name(self):
        n1 = Node("Sat1")
        n2 = Node("Sat1")
        assert n1 == n2

    def test_not_equal_different_names(self):
        n1 = Node("Sat1")
        n2 = Node("Sat2")
        assert n1 != n2

    def test_not_equal_to_non_node_returns_false(self):
        n = Node("Sat1")
        assert n.__eq__("Sat1") is False

    def test_hash_equal_for_same_name(self):
        n1 = Node("Sat1")
        n2 = Node("Sat1")
        assert hash(n1) == hash(n2)

    def test_hash_equals_hash_of_name(self):
        n = Node("Sat1")
        assert hash(n) == hash("Sat1")

    def test_usable_as_dict_key(self):
        n1 = Node("Sat1")
        n2 = Node("Sat1")
        d = {n1: "value"}
        assert d[n2] == "value"


class TestNodeStringRepresentation:
    """Tests for Node.__str__ and __repr__."""

    def test_str_returns_name(self):
        node = Node("Sat3")
        assert str(node) == "Sat3"

    def test_repr_returns_name(self):
        node = Node("Sat3")
        assert repr(node) == "Sat3"


class TestNodeHashNode:
    """Tests for Node.hash_node."""

    def test_returns_integer(self):
        node = Node("Sat0")
        node.position = {"latitude": 10.0, "longitude": 20.0, "altitude": 550.0}
        assert isinstance(node.hash_node(), int)

    def test_different_positions_produce_different_hashes(self):
        n1 = Node("Sat0")
        n1.position = {"latitude": 10.0, "longitude": 20.0, "altitude": 550.0}
        n2 = Node("Sat0")
        n2.position = {"latitude": 11.0, "longitude": 20.0, "altitude": 550.0}
        assert n1.hash_node() != n2.hash_node()

    def test_same_position_same_name_same_hash(self):
        n1 = Node("Sat0")
        n1.position = {"latitude": 10.0, "longitude": 20.0, "altitude": 550.0}
        n2 = Node("Sat0")
        n2.position = {"latitude": 10.0, "longitude": 20.0, "altitude": 550.0}
        assert n1.hash_node() == n2.hash_node()
