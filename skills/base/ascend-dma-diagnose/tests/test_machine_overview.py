"""Offline contract tests for a compact whole-machine I/O overview.

The oracle follows recorded immediate parents directly; it does not reuse any
layout projection. NIC/NVMe may be summarized, but no BDF may disappear from
their visible text and no summarized connection may become a shortcut edge.
"""
import copy
import json
import math
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
from dmalib import layout  # noqa: E402

SVG_NS = "http://www.w3.org/2000/svg"
NS = {"s": SVG_NS}


def device(bdf, parent=None, *, kind="bridge", role="bridge", root="0000:00", socket=0):
    return dict(bdf=bdf, parent_bdf=parent, root_bus=root, cpu_socket=socket, numa_node=socket,
                kind=kind, role=role, name="Synthetic " + kind,
                dsn="repeated-dsn-is-not-an-asic-id",
                link=dict(current_speed_gt_s=32, current_width=16, max_speed_gt_s=32, max_width=16),
                evidence_refs=["synthetic-pci-parent"])


def small_topology():
    # Nonconsecutive bus numbers deliberately make BDF arithmetic an invalid
    # substitute for the actual PCI parent relation.
    devices = [
        device("0000:00:01.0", role="root_port"),
        device("0000:11:00.0", "0000:00:01.0", role="switch_upstream"),
        device("0000:12:00.0", "0000:11:00.0", role="switch_downstream"),
        device("0000:12:01.0", "0000:11:00.0", role="switch_downstream"),
        device("0000:31:00.0", "0000:12:00.0", kind="npu", role="endpoint"),
        device("0000:07:00.0", "0000:12:01.0", kind="npu", role="endpoint"),
        # An independent root port on the same logical RootBus.
        device("0000:00:02.0", role="root_port"),
        device("0000:55:00.0", "0000:00:02.0", kind="npu", role="endpoint"),
        device("0000:12:02.0", "0000:11:00.0", role="switch_downstream"),
        device("0000:09:00.0", "0000:12:02.0", kind="nic", role="endpoint"),
        device("0000:00:03.0", role="root_port"),
        device("0000:32:00.0", "0000:00:03.0", kind="nvme", role="endpoint"),
        device("0000:80:02.0", role="root_port", root="0000:80", socket=1),
        device("0000:81:00.0", "0000:80:02.0", role="switch_upstream", root="0000:80", socket=1),
        device("0000:82:00.0", "0000:81:00.0", role="switch_downstream", root="0000:80", socket=1),
        device("0000:83:00.0", "0000:82:00.0", kind="npu", role="endpoint", root="0000:80", socket=1),
        device("0000:80:03.0", role="root_port", root="0000:80", socket=1),
        device("0000:84:00.0", "0000:80:03.0", kind="nic", role="endpoint", root="0000:80", socket=1),
        device("0000:80:04.0", role="root_port", root="0000:80", socket=1),
        device("0000:85:00.0", "0000:80:04.0", kind="nvme", role="endpoint", root="0000:80", socket=1),
        device("0000:00:1f.0", kind="host", role="host"),
        device("0000:80:1f.0", kind="other", role="other", root="0000:80", socket=1),
    ]
    npus = [d["bdf"] for d in devices if d["kind"] == "npu"]
    return dict(schema_version="1.0", host="synthetic-machine", cpu=dict(sockets=2, model_name="Synthetic CPU"),
                numa_nodes=[dict(id=i, socket=i, cpus=[i]) for i in range(2)],
                roots=[dict(root_bus="0000:00", cpu_socket=0, numa_node=0),
                       dict(root_bus="0000:80", cpu_socket=1, numa_node=1)],
                devices={d["bdf"]: d for d in devices},
                npu_devices=[dict(logical_id=i, physical_id=i + 8, bdf=bdf, mapping_verified=True,
                                  mapping_source="explicit") for i, bdf in enumerate(npus)],
                io_mapping=[], commands=[], warnings=[])


def kind_bdfs(topology, kind):
    return {bdf for bdf, item in topology["devices"].items() if item.get("kind") == kind}


def npu_ancestor_bdfs(topology):
    devices = topology["devices"]
    ancestors = set()
    for bdf in kind_bdfs(topology, "npu"):
        seen = {bdf}
        parent = devices[bdf].get("parent_bdf")
        while parent in devices and parent not in seen:
            ancestors.add(parent)
            seen.add(parent)
            parent = devices[parent].get("parent_bdf")
    return ancestors


def recorded_edges(topology, members=None):
    devices = topology["devices"]
    members = set(devices) if members is None else set(members)
    return {(item.get("parent_bdf"), bdf) for bdf, item in devices.items()
            if bdf in members and item.get("parent_bdf") in members and item.get("parent_bdf") != bdf}


def elements_with_class(svg, name):
    return [item for item in svg.iter() if name in item.get("class", "").split()]


def visible_text(svg):
    # <title>, <desc> and data attributes alone do not make a BDF visible.
    return " ".join("".join(item.itertext()) for item in svg.iter("{" + SVG_NS + "}text"))


def node_box(node):
    keys = ("x", "y", "width", "height")
    if all(node.get("data-" + key) is not None for key in keys):
        return tuple(float(node.get("data-" + key)) for key in keys)
    rect = node.find(".//s:rect", NS)
    if rect is None:
        raise AssertionError("machine-pci-node needs a measurable card rectangle: " + str(node.get("data-bdf")))
    return tuple(float(rect.get(key)) for key in keys)


class MachineOverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_topology = json.loads((SKILL / "examples" / "pcie-gen5-pairs.json").read_text(encoding="utf-8"))["topology"]

    def render(self, topology):
        text, meta = layout.machine_svg(topology)
        self.assertIsInstance(text, str)
        self.assertIsInstance(meta, dict)
        svg = ET.fromstring(text)
        self.assertEqual(svg.tag, "{" + SVG_NS + "}svg")
        return svg, meta

    def assert_graph_contract(self, topology, svg, meta):
        devices = topology["devices"]
        npus = kind_bdfs(topology, "npu")
        ancestors = npu_ancestor_bdfs(topology)
        nodes = elements_with_class(svg, "machine-pci-node")
        node_bdfs = {node.get("data-bdf") for node in nodes}
        self.assertEqual(len(nodes), len(node_bdfs), "Each PCI function needs its own single node")
        self.assertTrue(npus | ancestors <= node_bdfs, "All NPU root/up/down ancestors must be explicit")
        self.assertTrue(node_bdfs <= set(devices), "RootBus/socket context is not a PCI function")
        for node in nodes:
            self.assertTrue(node.tag.startswith("{" + SVG_NS + "}"))
            self.assertIn(node.get("data-bdf"), visible_text(node))

        edges = elements_with_class(svg, "machine-pci-edge")
        edge_pairs = {(edge.get("data-parent"), edge.get("data-child")) for edge in edges}
        self.assertEqual(len(edges), len(edge_pairs), "Do not duplicate a recorded parent edge")
        self.assertEqual(edge_pairs, recorded_edges(topology, node_bdfs),
                         "Render every immediate relation between explicit nodes; never shorten a parent chain")
        self.assertTrue(recorded_edges(topology, npus | ancestors) <= edge_pairs)
        self.assertTrue(edge_pairs <= recorded_edges(topology), "Every edge must exist in the source capture")
        for edge in edges:
            self.assertTrue(edge.tag.startswith("{" + SVG_NS + "}"))

        required = {"width", "height", "npu_bdfs", "context_bdfs", "nic_bdfs", "nvme_bdfs",
                    "exact_parent_edges", "omitted_bdfs"}
        self.assertTrue(required <= set(meta))
        self.assertEqual(set(meta["npu_bdfs"]), npus)
        self.assertEqual(set(meta["nic_bdfs"]), kind_bdfs(topology, "nic"))
        self.assertEqual(set(meta["nvme_bdfs"]), kind_bdfs(topology, "nvme"))
        context = set(meta["context_bdfs"])
        self.assertTrue(ancestors <= context)
        self.assertTrue(context <= node_bdfs)
        represented = npus | context | set(meta["nic_bdfs"]) | set(meta["nvme_bdfs"])
        self.assertTrue(node_bdfs <= represented, "Explicit cards cannot also be reported as omitted")
        self.assertEqual(set(meta["omitted_bdfs"]), set(devices) - represented)
        self.assertFalse(represented & set(meta["omitted_bdfs"]))
        self.assertEqual({tuple(pair) for pair in meta["exact_parent_edges"]}, edge_pairs)
        self.assertEqual(len(meta["exact_parent_edges"]), len(edge_pairs))

        text = visible_text(svg)
        for bdf in npus | kind_bdfs(topology, "nic") | kind_bdfs(topology, "nvme"):
            self.assertIn(bdf, text, "Endpoint BDF must be visible even when summarized")

    def assert_compact(self, svg, meta):
        width, height = meta["width"], meta["height"]
        self.assertTrue(all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0
                            for value in (width, height)))
        self.assertLessEqual(width, 2500)
        self.assertLessEqual(height, 2200)
        self.assertEqual(float(svg.get("width").removesuffix("px")), width)
        self.assertEqual(float(svg.get("height").removesuffix("px")), height)
        self.assertEqual([float(value) for value in svg.get("viewBox").split()], [0, 0, width, height])
        boxes = []
        for node in elements_with_class(svg, "machine-pci-node"):
            x, y, w, h = node_box(node)
            self.assertTrue(all(math.isfinite(value) for value in (x, y, w, h)))
            self.assertGreater(w, 0)
            self.assertGreater(h, 0)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, width)
            self.assertLessEqual(y + h, height)
            boxes.append((node.get("data-bdf"), x, y, w, h))
        for index, first in enumerate(boxes):
            for second in boxes[index + 1:]:
                _, x1, y1, w1, h1 = first
                _, x2, y2, w2, h2 = second
                self.assertTrue(x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 <= y2 or y2 + h2 <= y1,
                                "PCI cards must not overlap: " + first[0] + " / " + second[0])

    def assert_socket_columns(self, topology, svg):
        columns = {}
        for node in elements_with_class(svg, "machine-pci-node"):
            socket = topology["devices"][node.get("data-bdf")].get("cpu_socket")
            if socket is not None:
                x, _, w, _ = node_box(node)
                columns.setdefault(socket, []).append((x, x + w))
        self.assertEqual(set(columns), {0, 1})
        first, second = columns[0], columns[1]
        self.assertTrue(max(right for _, right in first) <= min(left for left, _ in second)
                        or max(right for _, right in second) <= min(left for left, _ in first),
                        "Known sockets must occupy separate horizontal columns")

    def test_small_fixture_preserves_full_npu_ancestry_and_endpoint_membership(self):
        top = small_topology()
        svg, meta = self.render(top)
        self.assert_graph_contract(top, svg, meta)
        self.assertIn("0000:00:1f.0", meta["omitted_bdfs"])
        self.assertIn("0000:80:1f.0", meta["omitted_bdfs"])

    def test_duplicate_dsn_never_merges_root_upstream_downstream_or_npu_functions(self):
        top = small_topology()
        svg, _ = self.render(top)
        nodes = elements_with_class(svg, "machine-pci-node")
        bdfs = [node.get("data-bdf") for node in nodes]
        expected = kind_bdfs(top, "npu") | npu_ancestor_bdfs(top)
        for bdf in expected:
            self.assertEqual(bdfs.count(bdf), 1, "Repeated DSN must preserve " + bdf)

    def test_root_bus_is_visible_logical_context_without_added_pci_edges(self):
        top = small_topology()
        svg, meta = self.render(top)
        text = visible_text(svg)
        for root in ("0000:00", "0000:80"):
            self.assertIn("RootBus " + root, text)
        self.assertRegex(text, r"逻辑|logical")
        edges = {tuple(pair) for pair in meta["exact_parent_edges"]}
        # These root ports share a RootBus, not a recorded wire or parent.
        self.assertNotIn(("0000:00:01.0", "0000:00:02.0"), edges)
        self.assertNotIn(("0000:00:02.0", "0000:00:01.0"), edges)
        self.assertTrue(edges <= recorded_edges(top))

    def test_small_fixture_is_compact_and_places_sockets_in_separate_columns(self):
        top = small_topology()
        svg, meta = self.render(top)
        self.assert_compact(svg, meta)
        self.assert_socket_columns(top, svg)

    def test_redacted_capture_covers_all_16_npus_18_nics_and_6_nvmes(self):
        top = self.real_topology
        self.assertEqual(len(kind_bdfs(top, "npu")), 16)
        self.assertEqual(len(kind_bdfs(top, "nic")), 18)
        self.assertEqual(len(kind_bdfs(top, "nvme")), 6)
        svg, meta = self.render(top)
        self.assert_graph_contract(top, svg, meta)
        self.assertEqual(len(meta["npu_bdfs"]), 16)
        self.assertEqual(len(meta["nic_bdfs"]), 18)
        self.assertEqual(len(meta["nvme_bdfs"]), 6)

    def test_redacted_capture_is_compact_and_keeps_socket_columns(self):
        top = self.real_topology
        svg, meta = self.render(top)
        self.assert_compact(svg, meta)
        self.assert_socket_columns(top, svg)

    def test_missing_parent_is_not_replaced_with_a_root_or_bdf_shortcut(self):
        top = small_topology()
        # Keep the downstream's parent reference as recorded but remove the
        # referenced upstream function, as can happen with incomplete capture.
        del top["devices"]["0000:11:00.0"]
        svg, meta = self.render(top)
        self.assert_graph_contract(top, svg, meta)
        self.assertNotIn("0000:11:00.0", {node.get("data-bdf") for node in elements_with_class(svg, "machine-pci-node")})
        edges = {tuple(pair) for pair in meta["exact_parent_edges"]}
        self.assertNotIn(("0000:00:01.0", "0000:12:00.0"), edges)
        self.assertNotIn(("0000:00:01.0", "0000:12:01.0"), edges)

    def test_rendering_does_not_mutate_source_topology(self):
        top = small_topology()
        before = copy.deepcopy(top)
        self.render(top)
        self.assertEqual(top, before)


if __name__ == "__main__":
    unittest.main()
