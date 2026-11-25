"""Dependency-free PCI hierarchy layout. No PCI ancestry is inferred from BDFs."""
from collections import Counter, defaultdict
from hashlib import sha256
from html import escape
import json
import math


COLORS = {
    "npu": ("#dcf5f1", "#259989"), "nic": ("#edeaff", "#8d7cc1"),
    "nvme": ("#e2efff", "#5b8fc4"), "bridge": ("#eef4e9", "#94ad76"),
    "host": ("#fff2ce", "#c9ab5b"), "other": ("#f2f4f7", "#aab6c4"),
    "group": ("#f3f5f9", "#a6b4c5"),
}
ROLE_NAMES = {"endpoint": "Endpoint", "root_port": "Root Port", "switch_upstream": "Switch Upstream",
              "switch_downstream": "Switch Downstream", "bridge": "Bridge", "host": "Host function"}


def node_id(bdf):
    return "dev-" + sha256(str(bdf).encode()).hexdigest()[:16]


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def link_text(link, maximum=False):
    prefix = "max" if maximum else "current"
    speed, width = link.get(prefix + "_speed_gt_s"), link.get(prefix + "_width")
    generations = {2.5: 1, 5: 2, 8: 3, 16: 4, 32: 5, 64: 6}
    gen = generations.get(speed) if _number(speed) else None
    speed_label = ("Gen" + str(gen)) if gen else (f"{speed:g} GT/s" if _number(speed) else "速率未采集")
    return f"{speed_label} ×{width}" if _number(width) else speed_label + " · 宽度未采集"


def link_ceiling(link):
    speed, width = link.get("current_speed_gt_s"), link.get("current_width")
    if not (_number(speed) and _number(width) and speed > 0 and width > 0):
        return None
    # Gen6 uses FLIT/FEC: do not extrapolate Gen3–5 encoding to an unknown mode.
    if speed in (2.5, 5):
        return speed * width * 0.8 / 8
    if speed in (8, 16, 32):
        return speed * width * 128 / 130 / 8
    return None


def _short(text, units=37):
    """Bound label width with CJK characters occupying approximately two cells."""
    out, used = [], 0
    for char in str(text):
        cost = 2 if ord(char) > 0x2FF else 1
        if used + cost > units:
            return "".join(out) + "…"
        out.append(char)
        used += cost
    return "".join(out)


def graph(topology, overview=False):
    """Project a PCI forest; overview groups only non-bridge host/other leaves."""
    devices = {str(bdf): dict(device, bdf=str(bdf)) for bdf, device in topology.get("devices", {}).items()}
    children = defaultdict(list)
    warnings = []
    for bdf, device in devices.items():
        parent = device.get("parent_bdf")
        if parent in devices and parent != bdf:
            children[parent].append(bdf)
        elif parent:
            warnings.append(f"{bdf}: 父节点 {parent} 缺失或自引用；不补画猜测链路。")
    groups, hidden = [], set()
    if overview:
        candidates = defaultdict(list)
        for bdf, device in devices.items():
            if (not children[bdf] and device.get("kind", "other") in ("host", "other")
                    and device.get("role") not in ("root_port", "switch_upstream", "switch_downstream", "bridge")):
                candidates[(device.get("root_bus"), device.get("parent_bdf"))].append(bdf)
        for (root, parent), members in sorted(candidates.items(), key=lambda item: str(item[0])):
            if len(members) < 3:
                continue
            members = sorted(members)
            key = "group-" + sha256(json.dumps(members).encode()).hexdigest()[:12]
            groups.append({"bdf": key, "kind": "group", "role": "group", "root_bus": root,
                           "parent_bdf": parent, "members": members,
                           "name": f"{len(members)} 个 Host / Other 叶子函数"})
            hidden.update(members)
    visible = {bdf: device for bdf, device in devices.items() if bdf not in hidden}
    visible.update({group["bdf"]: group for group in groups})
    root_info = {str(root.get("root_bus")): dict(root) for root in topology.get("roots", [])}
    for device in visible.values():
        root = str(device.get("root_bus") or "unknown")
        if root not in root_info:
            root_info[root] = {"root_bus": root, "cpu_socket": None, "numa_node": None}
    actual_edges = [(device.get("parent_bdf"), bdf) for bdf, device in devices.items()
                    if device.get("parent_bdf") in devices and device.get("parent_bdf") != bdf]
    edges = [(device.get("parent_bdf"), bdf) for bdf, device in visible.items()
             if device.get("parent_bdf") in visible and device.get("parent_bdf") != bdf]
    return dict(devices=devices, nodes=visible, roots=root_info, edges=edges, actual_edges=actual_edges,
                groups=groups, hidden=hidden, warnings=warnings)


def _npu_descendants(bdf, children, devices, trail=None):
    trail = set() if trail is None else trail
    if bdf in trail:
        return set()
    trail = trail | {bdf}
    out = {bdf} if devices[bdf].get("kind") == "npu" else set()
    for child in children[bdf]:
        out.update(_npu_descendants(child, children, devices, trail))
    return out


def _labels(device, mappings, io_map, compact=False):
    kind, role = device.get("kind", "other"), device.get("role", "other")
    if kind == "group":
        return [device["name"], "概览折叠 · 非物理设备", "精确成员见详情 / 全树", _short(", ".join(device["members"]), 38)]
    role_label = ROLE_NAMES.get(role, role)
    if device.get("role_source") == "pci_class_inference":
        role_label += " · 类别推定"
    lines = [_short(f"{kind.upper()} · {role_label}", 43), device["bdf"]]
    if not compact or kind in ("nic", "nvme"):
        lines.append(_short(device.get("name") or "名称未采集"))
    npu = mappings.get(device["bdf"], [])
    if npu:
        ids = [f"L{m.get('logical_id', '?')} / P{m.get('physical_id', '?')}" for m in npu]
        lines.append(_short("NPU " + ", ".join(ids)))
        if not all(m.get("mapping_verified") is True for m in npu):
            lines.append("逻辑卡 ↔ BDF 映射未验证")
    link = device.get("link") or {}
    if link or role in ("root_port", "switch_upstream", "switch_downstream", "endpoint"):
        lines.extend([_short(link_text(link) + " / max " + link_text(link, True), 43)] if compact else
                     ["当前 " + link_text(link), "能力 " + link_text(link, True)])
    socket, numa = device.get("cpu_socket"), device.get("numa_node")
    lines.append(f"Socket {socket if socket is not None else '?'} · NUMA {numa if numa is not None else '?'}")
    for item in io_map.get(device["bdf"], []):
        unit = item.get("unit_id")
        unit_label = str(unit) if unit is not None else "?"
        if unit is None and item.get("unit_candidates"):
            unit_label += " 候选 " + "/".join(map(str, item["unit_candidates"]))
        lines.append(_short(f"{item.get('unit_kind', 'I/O')} {unit_label} · {item.get('confidence', 'unknown')}"))
    if device.get("dsn") and not compact:
        lines.append(_short("DSN " + str(device["dsn"])))
    return [_short(line, 35) for line in lines] if compact else lines


def svg(topology, overview=False, href_prefix="report.html", compact=False):
    """Return (SVG text, graph metadata) with one node per visible PCI function."""
    model = graph(topology, overview)
    nodes, children = model["nodes"], defaultdict(list)
    for parent, child in model["edges"]:
        children[parent].append(child)
    for values in children.values():
        values.sort()
    mappings, io_map = defaultdict(list), defaultdict(list)
    for mapping in topology.get("npu_devices", []):
        mappings[str(mapping.get("bdf"))].append(mapping)
    for mapping in topology.get("io_mapping", []):
        io_map[str(mapping.get("bdf"))].append(mapping)
    labels = {bdf: _labels(device, mappings, io_map, compact) for bdf, device in nodes.items()}
    box_width, step, gap = (250, 355, 18) if compact else (300, 450, 26)
    x_origin = 300 if compact else 360
    heights = {bdf: 30 + len(lines) * 18 for bdf, lines in labels.items()}
    positions, visited, panels = {}, set(), []
    cursor, max_depth = 114 if compact else 182, 1
    # Panels follow actual root-bus membership. RootBus is context, not a wire.
    for root, info in sorted(model["roots"].items()):
        members = {bdf for bdf, device in nodes.items() if str(device.get("root_bus") or "unknown") == root}
        tops = sorted(bdf for bdf in members if nodes[bdf].get("parent_bdf") not in members)
        panel_start = cursor
        cursor += 90 if compact else 114

        def place(bdf, depth, y, trail):
            nonlocal max_depth
            if bdf in visited:
                if bdf in trail:
                    model["warnings"].append(f"{bdf}: PCI 父链存在环；保留关系但不作物理拓扑推断。")
                return 0
            visited.add(bdf)
            max_depth = max(max_depth, depth)
            start = y
            child_centers = []
            for child in children[bdf]:
                if child not in members:
                    continue
                occupied = place(child, depth + 1, y, trail | {bdf})
                if occupied:
                    child_centers.append(positions[child][1] + heights[child] / 2)
                    y += occupied + gap
            occupied = max(heights[bdf], y - start - gap if child_centers else heights[bdf])
            center = (child_centers[0] + child_centers[-1]) / 2 if child_centers else start + heights[bdf] / 2
            center = max(start + heights[bdf] / 2, min(center, start + occupied - heights[bdf] / 2))
            positions[bdf] = (x_origin + depth * step, center - heights[bdf] / 2)
            return occupied

        for bdf in tops + sorted(members):
            if bdf not in visited:
                cursor += place(bdf, 0, cursor, set()) + gap
        if not members:
            cursor += 84
        panels.append(dict(root=root, info=info, start=panel_start, end=cursor + 24, members=members))
        cursor += 58
    width, height = max(1320, x_origin + max_depth * step + box_width + 80), max(550, cursor + 72)
    counts = Counter(device.get("kind", "other") for device in model["devices"].values())
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
             f'role="img" aria-labelledby="topology-title topology-desc" data-pci-node-count="{len(model["devices"])}" '
             f'data-visible-pci-node-count="{len(nodes) - len(model["groups"])}" data-pci-edge-count="{len(model["actual_edges"])}" '
             f'data-rendered-edge-count="{len(model["edges"])}" data-view="{"focus" if compact else "overview" if overview else "full"}">',
             '<title id="topology-title">PCIe 可观测父子拓扑</title>',
             '<desc id="topology-desc">每个 PCI function 独立显示；根总线是逻辑归属，不代表共享物理线缆；DSN 不合并芯片。'
             '链路标签为端口报告，PCI 层级内部边不冒充物理线缆。完整 BDF、命令和原始证据在报告详情。</desc>',
             '<style>text{font-family:ui-sans-serif,system-ui,-apple-system,"Noto Sans CJK SC",sans-serif;fill:#1c3046}'
             '.muted{fill:#5f748b}.node{cursor:pointer}.node:hover rect{stroke-width:3}.shared{stroke:#d18130}'
             '.wire{stroke:#8ca3b9}.context{stroke:#aab7c7;stroke-dasharray:6 5}.edge-label{font-size:11px;fill:#52677e;paint-order:stroke;stroke:#f6f9fc;stroke-width:5px;stroke-linejoin:round}</style>',
             f'<rect width="{width}" height="{height}" fill="#f6f9fc"/>',
             f'<text x="38" y="{38 if compact else 48}" font-size="{23 if compact else 27}" font-weight="750">{escape(str(topology.get("host", "主机未采集")))} · PCIe 拓扑 / {"关键端点局部页" if compact else "概览" if overview else "完整树"}</text>',
             f'<text x="40" y="{66 if compact else 80}" font-size="{12 if compact else 14}" class="muted">{len(model["devices"])} PCI functions · '
             f'{len(model["actual_edges"])} 已知父子关系 · NPU {counts["npu"]} / NIC {counts["nic"]} / NVMe {counts["nvme"]}</text>',
             ('<text x="40" y="90" font-size="11" class="muted">局部投影：NPU / NIC / NVMe 与实际祖先；完整数据见全树。虚线为桥内部层级，不按 DSN 合并 ASIC。</text>' if compact else
              '<text x="40" y="110" font-size="13" class="muted">实线：端口相邻链路　虚线：逻辑归属 / 桥内部层级　橙色：多个 NPU 的共同上游路径（因果尚需实验）</text>'),
             '' if compact else '<text x="40" y="137" font-size="13" class="muted">同 RootBus ≠ 同一物理 PCIe 链路；DSN 仅作观测字段，绝不据此合并物理 ASIC。点击任一节点查看证据。</text>',
             '' if compact else '<text x="40" y="162" font-size="11" class="muted">measured: PCI / 端口记录　derived: 共同祖先 / 编码上界　inferred: 类别推定角色　unknown: 未采集 / 未验证</text>']
    for panel in panels:
        info, start, end = panel["info"], panel["start"], panel["end"]
        socket, numa = info.get("cpu_socket"), info.get("numa_node")
        parts.extend([f'<rect x="24" y="{start}" width="{width - 48}" height="{end - start}" rx="16" fill="#edf2f8" stroke="#dae4ef"/>',
                      f'<text x="44" y="{start + 31}" font-size="18" font-weight="700">RootBus {escape(panel["root"])}</text>',
                      f'<text x="44" y="{start + 56}" font-size="13" class="muted">Socket {escape(str(socket if socket is not None else "?"))} · NUMA {escape(str(numa if numa is not None else "?"))} · {len(panel["members"])} 可见节点</text>',
                      f'<text x="44" y="{start + 80}" font-size="12" class="muted">RootBus 为 Linux 逻辑根总线</text>'])
        # A socket/NUMA box is association context, never a measured wire.
        cpu_name = _short(topology.get("cpu", {}).get("model_name") or "CPU 型号未采集", 30 if compact else 35)
        parts.extend([f'<rect x="44" y="{start + 115}" width="{220 if compact else 274}" height="102" rx="12" fill="#fff1c9" stroke="#d5bd77"/>',
                      f'<text x="59" y="{start + 141}" font-size="{12 if compact else 14}" font-weight="700">{"" if compact else "CPU "}Socket {escape(str(socket if socket is not None else "?"))} / NUMA {escape(str(numa if numa is not None else "?"))}</text>',
                      f'<text x="59" y="{start + 166}" font-size="12">{escape(cpu_name)}</text>',
                      f'<text x="59" y="{start + 192}" font-size="12" class="muted">NUMA / 固件归属，非物理线速</text>'])
    descendants = {bdf: _npu_descendants(bdf, children, nodes) for bdf in nodes}
    for parent, child in model["edges"]:
        px, py = positions[parent]; cx, cy = positions[child]
        py += heights[parent] / 2; cy += heights[child] / 2
        shared = len(descendants[child]) > 1
        parent_role, child_role = nodes[parent].get("role"), nodes[child].get("role")
        internal = parent_role == "switch_upstream" and child_role == "switch_downstream"
        wire = not internal and parent_role in ("root_port", "switch_downstream") and child_role in (
            "endpoint", "switch_upstream", "root_port", "bridge")
        css = "shared" if shared else ("wire" if wire else "context")
        label = link_text(nodes[child].get("link") or {}) if wire else ("交换桥内部层级" if internal else "PCI 父子关系")
        if shared:
            label = f"NPU×{len(descendants[child])} / " + label if compact else label + f" · NPU×{len(descendants[child])}"
        x1, x2 = px + box_width, cx
        middle = x1 + (x2 - x1) / 2
        parts.append(f'<g class="pci-edge {css}" data-parent="{escape(parent, quote=True)}" data-child="{escape(child, quote=True)}" '
                     f'data-relation="{"link" if wire else "hierarchy"}" data-evidence-kind="measured" '
                     f'data-classification-kind="derived"><title>{escape(parent)} → {escape(child)}; '
                     f'measured: actual immediate PCI parent; derived: rendering style from reported port roles; '
                     f'physical ASIC identity unknown; evidence: {escape(str(nodes[child].get("evidence_refs", [])))}</title>'
                     f'<path d="M{x1},{py} H{middle} V{cy} H{x2}" fill="none" stroke-width="{3 if shared else 1.8}"/>'
                     f'<text x="{x1 + 8}" y="{cy - 10}" class="edge-label">{escape(_short(label, 16 if compact else 22))}</text></g>')
    for bdf, device in nodes.items():
        x, y = positions[bdf]
        fill, border = COLORS.get(device.get("kind"), COLORS["other"])
        key = node_id(bdf)
        tag = "group" if device.get("kind") == "group" else "pci-node"
        # Full capture fields remain in the report's device details/topology.json.
        # Keep SVG hover text useful without repeating large AER/config metadata.
        details = {key: device.get(key) for key in ("bdf", "parent_bdf", "root_bus", "kind", "role", "role_source",
                                                   "name", "link", "dsn", "evidence_refs", "members") if key in device}
        parts.append(f'<a class="node {tag}" href="{escape(href_prefix, quote=True)}#{key}" data-bdf="{escape(bdf, quote=True)}" '
                     f'data-members="{escape(json.dumps(device.get("members", [])), quote=True)}" id="svg-{key}" '
                     f'data-x="{x}" data-y="{y}" data-width="{box_width}" data-height="{heights[bdf]}">'
                     f'<title>{escape(json.dumps(details, ensure_ascii=False, indent=2))}</title>'
                     f'<rect x="{x}" y="{y}" width="{box_width}" height="{heights[bdf]}" rx="12" fill="{fill}" stroke="{border}" stroke-width="1.5"/>'
                     f'<rect x="{x}" y="{y + 14}" width="4" height="{heights[bdf] - 28}" rx="2" fill="{border}"/>')
        for index, label in enumerate(labels[bdf]):
            parts.append(f'<text x="{x + 16}" y="{y + 24 + index * 18}" font-size="{15 if index == 1 else 12}" '
                         f'font-weight="{700 if index in (0, 1) else 400}">{escape(label)}</text>')
        parts.append('</a>')
    parts.append('</svg>')
    metadata = dict(pci_nodes=len(model["devices"]), pci_edges=len(model["actual_edges"]),
                    visible_pci_nodes=len(nodes) - len(model["groups"]), rendered_edges=len(model["edges"]),
                    grouped_pci_nodes=len(model["hidden"]), groups=model["groups"], warnings=model["warnings"],
                    width=width, height=height, boxes={bdf: dict(x=pos[0], y=pos[1], width=box_width, height=heights[bdf])
                                                       for bdf, pos in positions.items()})
    return "".join(parts), metadata


def focus_pages(topology):
    """Page only key endpoints and their actual ancestors, preserving all sources.

    The focus projection is explicitly incomplete. The full and membership
    overview projections remain unchanged, and every omitted BDF is disclosed.
    """
    devices = topology.get("devices", {})
    critical = {bdf for bdf, device in devices.items() if device.get("kind") in ("npu", "nic", "nvme")}
    included = set(critical)
    for bdf in sorted(critical):
        trail = set()
        while bdf in devices and bdf not in trail:
            included.add(bdf)
            trail.add(bdf)
            bdf = devices[bdf].get("parent_bdf")
    root_info = {str(root.get("root_bus")): root for root in topology.get("roots", [])}
    roots = sorted({str(devices[bdf].get("root_bus") or "unknown") for bdf in included})
    npu_ids = defaultdict(list)
    for mapping in topology.get("npu_devices", []):
        if mapping.get("bdf") in devices:
            root = str(devices[mapping["bdf"]].get("root_bus") or "unknown")
            npu_ids[root].append(mapping.get("logical_id"))
    roots.sort(key=lambda root: (not bool(npu_ids[root]),
                                min((value for value in npu_ids[root] if type(value) is int), default=10**9), root))
    pages = []
    for root in roots:
        members = sorted(bdf for bdf in included if str(devices[bdf].get("root_bus") or "unknown") == root)
        source_members = sorted(bdf for bdf, device in devices.items() if str(device.get("root_bus") or "unknown") == root)
        page_topology = dict(topology, devices={bdf: devices[bdf] for bdf in members},
                             roots=[root_info.get(root, dict(root_bus=root, cpu_socket=None, numa_node=None))],
                             npu_devices=[item for item in topology.get("npu_devices", []) if item.get("bdf") in members],
                             io_mapping=[item for item in topology.get("io_mapping", []) if item.get("bdf") in members])
        # A cross-root ancestor remains an observed edge in the full graph; do
        # not add a shortened, synthetic edge in this root-specific projection.
        image, meta = svg(page_topology, compact=True)
        page_id = "root-" + sha256(root.encode()).hexdigest()[:12]
        pages.append(dict(id=page_id, root_bus=root, npu_ids=npu_ids[root],
                          counts=dict(Counter(devices[bdf].get("kind", "other") for bdf in members)),
                          members=members, source_members=source_members,
                          omitted_members=sorted(set(source_members) - set(members)), svg=image, graph=meta))
    return dict(pages=pages, members=sorted(included),
                omitted_members=sorted(set(devices) - included), source_pci_nodes=len(devices),
                source_pci_edges=sum(device.get("parent_bdf") in devices and device.get("parent_bdf") != bdf
                                     for bdf, device in devices.items()),
                note="Focus shows NPU/NIC/NVMe and actual PCI ancestors only. All omitted BDFs remain in full/overview/details; no shortcut edges.")


def machine_svg(topology):
    """Compact machine-wide NPU ancestor skeleton plus exact NIC/NVMe inventory.

    Only immediate PCI parent edges between explicit cards are drawn. RootBus,
    socket panels and inventory sidebars are logical context, never shortcut
    wires. Deep hierarchies fold into additional vertical bands without omitting
    ancestors; large machines therefore grow vertically rather than hiding data.
    """
    devices = topology.get("devices", {})
    npu = {bdf for bdf, device in devices.items() if device.get("kind") == "npu"}
    nic = {bdf for bdf, device in devices.items() if device.get("kind") == "nic"}
    nvme = {bdf for bdf, device in devices.items() if device.get("kind") == "nvme"}
    required, warnings = set(npu), []
    for bdf in sorted(npu):
        trail = set()
        while bdf in devices and bdf not in trail:
            required.add(bdf); trail.add(bdf)
            parent = devices[bdf].get("parent_bdf")
            if parent and parent not in devices:
                warnings.append(f"{bdf}: missing parent {parent}; no replacement edge")
            bdf = parent
        if bdf in trail:
            warnings.append(f"{bdf}: cyclic PCI parent chain; inspect full evidence")
    exact_edges = sorted((devices[bdf].get("parent_bdf"), bdf) for bdf in required
                         if devices[bdf].get("parent_bdf") in required and devices[bdf].get("parent_bdf") != bdf)
    root_info = {str(root.get("root_bus")): root for root in topology.get("roots", [])}
    mappings, io = defaultdict(list), defaultdict(list)
    for item in topology.get("npu_devices", []):
        mappings[item.get("bdf")].append(item)
    for item in topology.get("io_mapping", []):
        io[item.get("bdf")].append(item)

    def root_of(bdf):
        return str(devices[bdf].get("root_bus") or "unknown")

    def socket_of_root(root):
        recorded = root_info.get(root, {}).get("cpu_socket")
        if recorded is not None:
            return recorded
        values = {devices[bdf].get("cpu_socket") for bdf in required if root_of(bdf) == root
                  and devices[bdf].get("cpu_socket") is not None}
        return next(iter(values)) if len(values) == 1 else None

    def rank_key(bdf):
        return min((item["logical_id"] for item in mappings[bdf] if type(item.get("logical_id")) is int), default=10**9), bdf

    def io_label(bdfs):
        labels = set()
        for bdf in bdfs:
            for item in io[bdf]:
                unit = item.get("unit_id")
                label = f"{item.get('unit_kind', 'I/O')} {unit if unit is not None else '?'}"
                if unit is None and item.get("unit_candidates"):
                    label += " candidates " + "/".join(map(str, item["unit_candidates"]))
                labels.add(label + " " + str(item.get("confidence", "unknown")))
        return "; ".join(sorted(labels)) or "I/O unit unknown"

    root_groups = defaultdict(list)
    for bdf in npu:
        root_groups[root_of(bdf)].append(bdf)
    socket_roots = defaultdict(list)
    for root in root_groups:
        socket_roots[socket_of_root(root)].append(root)
    inventory_roots = defaultdict(lambda: defaultdict(list))
    for bdf in sorted(nic | nvme):
        socket = devices[bdf].get("cpu_socket")
        if socket is None:
            socket = root_info.get(root_of(bdf), {}).get("cpu_socket")
        inventory_roots[socket][root_of(bdf)].append(bdf)
    sockets = sorted(set(socket_roots) | set(inventory_roots), key=lambda value: (value is None, str(value)))
    if not sockets:
        sockets = [None]
    columns, column_width, margin, column_gap = min(2, len(sockets)), 1050, 32, 28
    width = margin * 2 + columns * column_width + (columns - 1) * column_gap
    top, box_width, leaf_step, slots = 154, 164, 86, 4
    positions, plans, socket_plans, assigned = {}, [], [], set()
    current_y = top
    for row_start in range(0, len(sockets), columns):
        row_ends = []
        for column, socket in enumerate(sockets[row_start:row_start + columns]):
            x, y = margin + column * (column_width + column_gap), current_y
            socket_start = y
            y += 96
            roots = sorted(socket_roots[socket], key=lambda root: min(rank_key(bdf) for bdf in root_groups[root]))
            for root in roots:
                row_start_y = y
                row_npus = set(root_groups[root])
                members = set()
                for bdf in row_npus:
                    trail = set()
                    while bdf in devices and bdf not in trail:
                        members.add(bdf); trail.add(bdf); bdf = devices[bdf].get("parent_bdf")
                members -= assigned
                children = defaultdict(list)
                for bdf in members:
                    parent = devices[bdf].get("parent_bdf")
                    if parent in members and parent != bdf:
                        children[parent].append(bdf)
                def branch_key(bdf, trail=None):
                    trail = set() if trail is None else trail
                    if bdf in trail:
                        return rank_key(bdf)
                    candidates = [rank_key(bdf)] if bdf in row_npus else []
                    candidates.extend(branch_key(child, trail | {bdf}) for child in children.get(bdf, []))
                    return min(candidates, default=rank_key(bdf))
                for values in children.values():
                    values.sort(key=branch_key)
                tops = sorted((bdf for bdf in members if devices[bdf].get("parent_bdf") not in members), key=rank_key)
                depths, centers, seen = {}, {}, set()
                leaf_cursor = 0

                def walk(bdf, depth, trail):
                    nonlocal leaf_cursor
                    if bdf in seen:
                        return centers.get(bdf)
                    seen.add(bdf); depths[bdf] = depth
                    values = [walk(child, depth + 1, trail | {bdf}) for child in children[bdf] if child not in trail]
                    values = [value for value in values if value is not None]
                    if values:
                        center = (values[0] + values[-1]) / 2
                    else:
                        center = leaf_cursor * leaf_step + leaf_step / 2
                        leaf_cursor += 1
                    centers[bdf] = center
                    return center

                for bdf in tops + sorted(members, key=rank_key):
                    if bdf not in seen:
                        walk(bdf, 0, set())
                base_height = max(leaf_step, leaf_cursor * leaf_step)
                bands = max((depth // slots for depth in depths.values()), default=0) + 1
                for bdf, center in centers.items():
                    depth = depths[bdf]
                    height = 76 if bdf in npu else 64
                    px = x + 16 + (depth % slots) * 212
                    py = y + 42 + (depth // slots) * (base_height + 20) + center - height / 2
                    positions[bdf] = dict(x=px, y=py, width=box_width, height=height,
                                          depth=depth, band=depth // slots, band_base=y + 42,
                                          band_height=base_height + 20, column_x=x)
                assigned.update(members)
                inventory = inventory_roots[socket].get(root, [])
                row_height = max(42 + bands * (base_height + 20) + 18, 76 + len(inventory) * 43)
                plans.append(dict(root=root, socket=socket, x=x, y=y, height=row_height,
                                  members=members, npus=row_npus, inventory=inventory))
                y += row_height + 12
            extras = {root: values for root, values in inventory_roots[socket].items() if root not in roots}
            extra_y = y
            if extras:
                y += 32
                for root, values in sorted(extras.items()):
                    y += 27 + len(values) * 43 + 8
            socket_plans.append(dict(socket=socket, x=x, y=socket_start, end=y + 18,
                                     roots=roots, extra_y=extra_y, extras=extras))
            row_ends.append(y + 18)
        current_y = max(row_ends) + 28
    height = max(420, current_y + 80)
    css = ('text{font-family:ui-sans-serif,system-ui,-apple-system,"Noto Sans CJK SC",sans-serif;fill:#20364a}'
           '.machine-pci-node,.machine-inventory-member{cursor:pointer}'
           '.machine-pci-node:hover rect,.machine-inventory-member:hover rect{stroke-width:3}'
           '.machine-pci-edge path{fill:none;stroke:#8ea4b8;stroke-width:2}'
           '.machine-pci-edge.internal path{stroke-dasharray:5 4}'
           '.machine-pci-edge.shared path{stroke:#d18432;stroke-width:3}.muted{fill:#627b8f}')
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
             f'role="img" aria-label="整机 I/O 概览；完整 NPU 祖先链与 NIC NVMe 名录" data-view="machine" '
             f'data-npu-node-count="{len(npu)}" data-ancestor-node-count="{len(required - npu)}" data-exact-edge-count="{len(exact_edges)}" '
             f'data-source-pci-node-count="{len(devices)}">',
             '<style>' + css + '</style>', f'<rect width="{width}" height="{height}" fill="#f6f9fc"/>',
             f'<text x="32" y="42" font-size="29" font-weight="750">{escape(str(topology.get("host", "主机")))} · 整机 I/O 概览</text>',
             f'<text x="34" y="72" font-size="15" class="muted">NPU {len(npu)} functions / NIC {len(nic)} / NVMe {len(nvme)} · 完整来源 {len(devices)} PCI functions；本图精确画出 {len(exact_edges)} 条 NPU 祖先父边</text>',
             '<text x="34" y="98" font-size="14" class="muted">按真实 socket 归属分列；RootBus 框仅逻辑分组，同 RootBus ≠ 共享一根 wire。所有祖先逐节点保留，不据 DSN 合并 ASIC。</text>',
             '<text x="34" y="123" font-size="13" class="muted">实线 / 虚线都来自实际 PCI parent；虚线区分桥内部层级。橙色只是多 NPU 共同上游；NIC/NVMe 列表不冒充直连。点击设备到细页。</text>']
    for panel in socket_plans:
        socket, x, y = panel["socket"], panel["x"], panel["y"]
        socket_npus = {bdf for root in panel["roots"] for bdf in root_groups[root]}
        numa = [str(item.get("id")) for item in topology.get("numa_nodes", []) if item.get("socket") == socket]
        parts.extend([f'<rect x="{x}" y="{y}" width="{column_width}" height="{panel["end"] - y}" rx="17" fill="#eaf0f7" stroke="#d2dfeb"/>',
                      f'<text x="{x + 18}" y="{y + 31}" font-size="25" font-weight="750">CPU Socket {escape(str(socket if socket is not None else "?"))} · NPU {len(socket_npus)}</text>',
                      f'<text x="{x + 18}" y="{y + 58}" font-size="14">{escape(_short(topology.get("cpu", {}).get("model_name") or "CPU 型号未知", 100))}</text>',
                      f'<text x="{x + 18}" y="{y + 80}" font-size="13" class="muted">NUMA {escape(", ".join(numa) or "?")} · {escape(_short(io_label(socket_npus), 125))}</text>'])
    for plan in plans:
        x, y = plan["x"], plan["y"]
        parts.extend([f'<rect x="{x + 10}" y="{y}" width="{column_width - 20}" height="{plan["height"]}" rx="10" fill="#fbfcfe" stroke="#cbd9e6" stroke-dasharray="5 4"/>',
                      f'<text x="{x + 23}" y="{y + 26}" font-size="17" font-weight="700">RootBus {escape(plan["root"])}</text>',
                      f'<text x="{x + 270}" y="{y + 26}" font-size="14" class="muted">{escape(_short(io_label(plan["npus"]), 100))} · logical grouping</text>'])
    full_children = defaultdict(list)
    for parent, child in exact_edges:
        full_children[parent].append(child)
    descendants = {bdf: _npu_descendants(bdf, full_children, devices) for bdf in required}
    for parent, child in exact_edges:
        a, b = positions[parent], positions[child]
        x1, y1 = a["x"] + a["width"], a["y"] + a["height"] / 2
        x2, y2 = b["x"], b["y"] + b["height"] / 2
        internal = devices[parent].get("role") == "switch_upstream" and devices[child].get("role") == "switch_downstream"
        shared = len(descendants[child]) > 1
        if a["band"] == b["band"] and x2 > x1:
            middle = (x1 + x2) / 2
            path = f"M{x1},{y1} H{middle} V{y2} H{x2}"
        else:
            right, left = a["column_x"] + 826, b["column_x"] + 12
            bottom = a["band_base"] + (a["band"] + 1) * a["band_height"] - 4
            path = f"M{x1},{y1} H{right} V{bottom} H{left} V{y2} H{x2}"
        parts.append(f'<g class="machine-pci-edge{" internal" if internal else ""}{" shared" if shared else ""}" '
                     f'data-parent="{escape(parent, quote=True)}" data-child="{escape(child, quote=True)}" '
                     f'data-evidence-kind="measured" data-classification-kind="derived"><title>{escape(parent)} → {escape(child)}; actual immediate PCI parent; '
                     f'{"internal hierarchy" if internal else "port adjacency"}; classification derived from reported roles</title><path d="{path}"/></g>')
    for bdf, box in positions.items():
        device = devices[bdf]
        x, y, w, h = box["x"], box["y"], box["width"], box["height"]
        fill, stroke = COLORS.get(device.get("kind"), COLORS["other"])
        ids = [str(item.get("logical_id", "?")) for item in mappings[bdf]]
        primary = ("NPU " + "/".join(ids)) if bdf in npu else ROLE_NAMES.get(device.get("role"), device.get("role", "PCI function"))
        primary = _short(primary, 23)
        attrs = " ".join(f'data-{key}="{box[key]}"' for key in ("x", "y", "width", "height"))
        tooltip = escape(json.dumps(dict(bdf=bdf, parent_bdf=device.get("parent_bdf"), root_bus=device.get("root_bus"),
                                         role=device.get("role"), mappings=mappings[bdf], io=io[bdf]), ensure_ascii=False))
        parts.extend([f'<a class="machine-pci-node" data-bdf="{escape(bdf, quote=True)}" {attrs} href="report.html#{node_id(bdf)}">'
                      f'<title>{tooltip}</title>',
                      f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{fill}" stroke="{stroke}"/>',
                      f'<text x="{x + 10}" y="{y + 20}" font-size="{19 if bdf in npu else 11}" font-weight="750">{escape(primary)}</text>',
                      f'<text x="{x + 10}" y="{y + 39}" font-size="12.5" font-weight="650">{escape(bdf)}</text>',
                      f'<text x="{x + 10}" y="{y + 55}" font-size="10.5" class="muted">{escape(_short(link_text(device.get("link") or {}), 26))}</text>'])
        if bdf in npu:
            parts.append(f'<text x="{x + 10}" y="{y + 69}" font-size="9.5" class="muted">{escape(_short(io_label([bdf]), 29))}</text>')
        parts.append('</a>')

    def inventory_card(bdf, x, y):
        device = devices[bdf]
        parent = device.get("parent_bdf")
        parts.append(f'<a class="machine-inventory-member" data-bdf="{escape(bdf, quote=True)}" href="report.html#{node_id(bdf)}">'
                     f'<title>{escape(json.dumps(dict(bdf=bdf, kind=device.get("kind"), parent_bdf=parent, root_bus=device.get("root_bus"), link=device.get("link")), ensure_ascii=False))}</title>'
                     f'<text x="{x}" y="{y}" font-size="11.5" font-weight="650">{escape(str(device.get("kind", "other")).upper())} {escape(bdf)}</text>'
                     f'<text x="{x}" y="{y + 15}" font-size="9.5" class="muted">{escape(_short(link_text(device.get("link") or {}) + " / parent " + str(parent or "?"), 42))}</text></a>')

    for plan in plans:
        if not plan["inventory"]:
            continue
        x, y = plan["x"] + 838, plan["y"]
        parts.extend([f'<rect x="{x - 9}" y="{y + 39}" width="195" height="{plan["height"] - 50}" rx="7" fill="#f0f4fa" stroke="#dbe3ec"/>',
                      f'<text x="{x}" y="{y + 56}" font-size="10" class="muted">同根 NIC / NVMe · 非直连线</text>'])
        for i, bdf in enumerate(plan["inventory"]):
            inventory_card(bdf, x, y + 78 + i * 43)
    for panel in socket_plans:
        if not panel["extras"]:
            continue
        x, y = panel["x"] + 20, panel["extra_y"] + 23
        parts.append(f'<text x="{x}" y="{y}" font-size="15" font-weight="700">其他 RootBus 的 NIC / NVMe 名录（不画猜测连线）</text>')
        y += 27
        for root, values in sorted(panel["extras"].items()):
            parts.append(f'<text x="{x}" y="{y}" font-size="12" class="muted">RootBus {escape(root)}</text>')
            y += 25
            for bdf in values:
                inventory_card(bdf, x + 8, y); y += 43
            y += 8
    parts.append(f'<text x="34" y="{height - 31}" font-size="13" class="muted">本图省略的 CPU 内置及非关键功能仍在 topology.svg / 精确概览 / 设备清单中。所有 NIC / NVMe BDF 均列出；本图不是物理 ASIC 芯片图。</text>')
    represented = required | nic | nvme
    meta = dict(width=width, height=height, npu_bdfs=sorted(npu), context_bdfs=sorted(required - npu),
                nic_bdfs=sorted(nic), nvme_bdfs=sorted(nvme), exact_parent_edges=exact_edges,
                omitted_bdfs=sorted(set(devices) - represented), warnings=warnings,
                source_pci_nodes=len(devices), source_pci_edges=len(graph(topology)["actual_edges"]),
                boxes=positions, note="Exact NPU ancestor edges; NIC/NVMe are inventory lists, never synthetic CPU links. RootBus panels are logical grouping.")
    membership = {key: meta[key] for key in ("npu_bdfs", "context_bdfs", "nic_bdfs", "nvme_bdfs", "omitted_bdfs", "exact_parent_edges")}
    parts.append('<metadata id="machine-membership">' + escape(json.dumps(membership, ensure_ascii=False)) + '</metadata></svg>')
    return "".join(parts), meta
