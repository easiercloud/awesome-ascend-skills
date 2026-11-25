"""Render an offline, evidence-linked PCIe / DMA diagnostic report.

Only the Python standard library is required. Rendering never launches commands,
loads an accelerator framework, or upgrades a topology observation to causality.
"""
import csv
import datetime as dt
from hashlib import sha256
from html import escape
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex
from urllib.parse import quote

from . import layout


ASSETS = Path(__file__).resolve().parents[2] / "assets"
STATUS = {"confirmed": "已确认", "supported": "有证据支持", "refuted": "已反驳", "inconclusive": "未定论",
          "complete": "完成且验证", "unsupported": "不支持", "failed": "失败", "busy": "资源占用",
          "interrupted": "中断", "unvalidated": "尚未验证", "unknown": "未知", "pending": "未完成",
          "partial": "部分覆盖", "unavailable": "不可用", "not_counted": "未计数"}
KNOWLEDGE = {
    "measured": ("采集 / 实测", "copy-loop 时间与字节、PCI 父子关系、端口当前状态；历史脱敏记录仍受公开证据范围限制。"),
    "derived": ("数值推导", "由采样或结构计算：bytes / wall、并发 / 单卡比、共同上游、Gen1–5 编码容量上界。"),
    "inferred": ("解释 / 推断", "机制解释、因果归因或按设备类别推定角色；匹配实验可加强它，不等于直接读到了内部物理结构。"),
    "unknown": ("尚未知晓", "缺失、未测、不支持或未验证的内容；不补成零，不借用另一台机器的映射。"),
}


def _asset_dir():
    # Original skill layout, archived reproduce-kit/{dmalib,assets}, or a
    # package-local assets copy. Importing this module never requires assets.
    for path in (ASSETS, Path(__file__).resolve().parents[1] / "assets", Path(__file__).resolve().parent / "assets"):
        if all((path / name).is_file() for name in ("report.html", "report.css", "report.js")):
            return path
    raise FileNotFoundError("Report assets missing: copy the skill's assets/ beside reproduce-kit/dmalib/ before rendering.")


def _text(value):
    if value is None:
        return "未采集"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _h(value):
    return escape(_text(value), quote=True)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str, allow_nan=False)


def _script_json(value):
    """Safe even for </script>, HTML comments, U+2028 and SVG payloads."""
    return (_json(value).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _safe_file(value):
    if not isinstance(value, str) or not value or re.search(r"[\\\x00-\x1f]", value):
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        return None
    return str(path)


def _raw_link(value, label=None):
    safe = _safe_file(value)
    return (f'<a href="{quote(safe, safe="/-._")}">{_h(label or value)}</a>'
            if safe else f'<span class="quiet">{_h(label or value)}</span>')


def _finite(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _fmt(value, digits=2):
    value = _finite(value)
    return "—" if value is None else f"{value:,.{digits}f}"


def _list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _id(prefix, value):
    return prefix + sha256(str(value).encode()).hexdigest()[:16]


def _badge(status):
    key = str(status or "unknown")
    # CSS class is chosen from a fixed allowlist, never from an input attribute.
    css = key if key in STATUS else "unknown"
    return f'<span class="pill {css}">{_h(STATUS.get(key, key))}</span>'


def _knowledge(kind):
    kind = kind if kind in KNOWLEDGE else "unknown"
    title, description = KNOWLEDGE[kind]
    return f'<span class="knowledge-tag knowledge-{kind}" data-knowledge-kind="{kind}" title="{_h(title)}；完整定义见报告概况">{kind} · {_h(title)}</span>'


def _knowledge_legend():
    return ('<div class="knowledge-legend"><div class="knowledge-intro"><strong>两条独立维度</strong>'
            '<p>来源类型回答“这条信息怎样得到”；实验判断回答“当前对照支持到哪一步”。'
            'inferred 与 confirmed 可以并存：后者是本轮实验决策，不把推断变成直接测量。</p></div>' +
            "".join(f'<div>{_knowledge(kind)}<p>{_h(description)}</p></div>' for kind, (_, description) in KNOWLEDGE.items()) + '</div>')


def _finding_kind(item):
    explicit = item.get("evidence_type", item.get("evidence_kind", item.get("knowledge_type")))
    if explicit in KNOWLEDGE:
        return explicit
    ident = str(item.get("id", ""))
    if ident.startswith("link-"):
        return "measured"
    if ident.startswith(("scaling-", "capacity-", "duplex-", "hd-", "event-", "submit-")):
        return "derived"
    return "inferred"


def _paragraph(value):
    if isinstance(value, (dict, list)):
        return f'<pre>{_h(_json(value))}</pre>'
    return f'<p>{_h(value).replace(chr(10), "<br>")}</p>'


def _inline_svg(svg, view):
    return (svg.replace('href="report.html#', 'href="#')
            .replace('id="svg-', f'id="{view}-svg-')
            .replace('id="topology-title"', f'id="{view}-topology-title"')
            .replace('id="topology-desc"', f'id="{view}-topology-desc"')
            .replace('aria-labelledby="topology-title topology-desc"',
                     f'aria-labelledby="{view}-topology-title {view}-topology-desc"'))


def _empty(message):
    return f'<div class="empty">{_h(message)}</div>'


def _md(value):
    return (escape(_text(value), quote=False).replace("\\", "\\\\").replace("|", "\\|")
            .replace("[", "\\[").replace("]", "\\]").replace("`", "\\`").replace("\r", " ").replace("\n", " "))


def _fence(text, language):
    text = str(text)
    fence = "`" * max(3, max((len(match) + 1 for match in re.findall(r"`+", text)), default=3))
    return [fence + language, text, fence]


def _csv_value(value):
    # A report exported to a spreadsheet must not execute a host-provided formula.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _commands(topology, out):
    commands = [dict(command) for command in topology.get("commands", []) if isinstance(command, dict)]
    # Some collectors keep command inventory separately to avoid bloating topology.
    if (not commands and (out / "commands.jsonl").is_file()
            and out.resolve() in (out / "commands.jsonl").resolve().parents):
        for line in (out / "commands.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                command = json.loads(line)
                if isinstance(command, dict):
                    commands.append(command)
            except ValueError:
                continue
    for index, command in enumerate(commands):
        command.setdefault("id", f"command-{index + 1}")
    return commands


def _command_text(command):
    argv = command.get("argv")
    if isinstance(argv, list):
        return shlex.join(str(arg) for arg in argv)
    return _text(argv or command.get("command") or "命令未记录")


def _preview(out, value):
    safe = _safe_file(value)
    if not safe:
        return "路径不可用或不在报告目录内。"
    path = (out / safe).resolve()
    try:
        path.relative_to(out.resolve())
        if not path.is_file():
            return "原始证据文件未随本报告提供。"
        with path.open("rb") as source:
            data = source.read(16001)
        text = data[:16000].decode("utf-8", errors="replace")
        return text + ("\n…预览截断；完整内容见原始文件。" if len(data) > 16000 else "")
    except (OSError, ValueError):
        return "原始证据文件无法读取。"


class Evidence:
    def __init__(self, out, topology, analysis):
        self.out = out
        self.commands = _commands(topology, out)
        self.entries = {str(command["id"]): command for command in self.commands}
        reads_path = out / "raw/sysfs-reads.json"
        try:
            reads = json.loads(reads_path.read_text(encoding="utf-8")) if (
                reads_path.is_file() and out.resolve() in reads_path.resolve().parents) else []
            read_index = {str(item["id"]): item for item in reads if isinstance(item, dict) and "id" in item}
        except (OSError, ValueError, TypeError):
            read_index = {}
        refs = []
        for device in topology.get("devices", {}).values():
            refs.extend(_list(device.get("evidence_refs")))
        for mapping in topology.get("io_mapping", []):
            refs.extend(_list(mapping.get("evidence_refs")))
        refs.extend(_list((topology.get("cpu") or {}).get("evidence_refs")))
        for collection in ("numa_nodes", "roots", "npu_devices"):
            for item in topology.get(collection, []):
                refs.extend(_list(item.get("evidence_refs")))
        for kind in ("findings", "hypotheses", "comparisons"):
            for item in analysis.get(kind, []):
                if isinstance(item, dict):
                    refs.extend(_list(item.get("evidence_refs")))
        for ref in refs:
            if isinstance(ref, str) and ref not in self.entries:
                filename, _, fragment = ref.partition("#")
                record = read_index.get(fragment) if filename == "raw/sysfs-reads.json" else None
                self.entries[ref] = dict(id=ref, source_file=filename if _safe_file(filename) else None,
                                        source_fragment=fragment or None, read_record=record)

    @staticmethod
    def read_value(record):
        if "bytes_hex" not in record:
            return "原始字节不可用: " + str(record.get("error") or record.get("status") or "not recorded")
        try:
            raw = bytes.fromhex(record.get("bytes_hex", ""))
        except (ValueError, TypeError):
            return "无法解码 bytes_hex；保留原始记录供核验。"
        if record.get("offset") is not None:
            return "raw bytes (hex): " + raw.hex()
        return raw.decode("utf-8", errors="replace")

    def links(self, refs):
        refs = _list(refs)
        if not refs:
            return '<span class="quiet">未提供逐项证据引用</span>'
        return '<div class="evidence-links">' + "".join(
            f'<a href="#{_id("ev-", ref)}">{_h(ref)}</a>' for ref in refs) + '</div>'

    def html(self):
        if not self.entries:
            return _empty("没有采集命令或证据引用；本报告无法补造其来源。")
        cards, source_cards = [], []
        missing_groups = {}
        missing_refs = set()
        for key, item in self.entries.items():
            filename = item.get("source_file")
            if not filename or item.get("read_record") or item.get("argv") or item.get("command"):
                continue
            safe = _safe_file(filename)
            path = (self.out / safe).resolve() if safe else None
            if path is None or not path.is_file() or self.out.resolve() not in path.parents:
                missing_groups.setdefault(filename, []).append(key)
                missing_refs.add(key)
        for key, item in self.entries.items():
            if key in missing_refs:
                continue
            content = [f'<article class="evidence-item" id="{_id("ev-", key)}"><h3>{_h(key)}</h3>']
            if item.get("argv") or item.get("command"):
                content.append(f'<pre>{_h(_command_text(item))}</pre><p>returncode: {_h(item.get("returncode"))} '
                               f'· started: {_h(item.get("started_at"))} · duration: {_h(item.get("duration_s"))} s</p>')
            record = item.get("read_record")
            if record:
                content.append(f'<p>只读来源: <code>{_h(record.get("path"))}</code> · status {_h(record.get("status"))}'
                               f' · offset {_h(record.get("offset"))} · size {_h(record.get("size"))}</p>'
                               f'<pre>{_h(self.read_value(record))}</pre>'
                               f'<p>SHA-256（采集时原始字节）: <code>{_h(record.get("sha256"))}</code></p>')
            for field in ("stdout_path", "stderr_path", "source_file"):
                if item.get(field):
                    content.append(f'<p>{_h(field)}: {_raw_link(item[field])}'
                                   f'{" · record " + _h(item.get("source_fragment")) if item.get("source_fragment") else ""}</p>')
                    if not record:
                        content.append(f'<details><summary>原始输出预览</summary><pre>{_h(_preview(self.out, item[field]))}</pre></details>')
            if item.get("sha256"):
                content.append(f'<p>SHA-256（采集记录）: <code>{_h(item["sha256"])}</code></p>')
            if not any(item.get(key) for key in ("stdout_path", "stderr_path", "source_file")):
                content.append('<p class="quiet">该引用尚未解析到原始输出文件；保留引用，证据缺口待补。</p>')
            card = "".join(content) + '</article>'
            (cards if item.get("argv") or item.get("command") else source_cards).append(card)
        for filename, refs in missing_groups.items():
            source_cards.append('<details class="evidence-item missing-source"><summary>' + _h(filename) +
                                f' · {len(refs)} 项引用 · 原始文件未公开 / 未附带</summary>' + _knowledge("unknown") +
                                '<p class="quiet">保留精确引用以核对结构化记录，但这些引用不能替代缺失的原始输出；不补造原始值。</p>'
                                '<div class="membership">' + "".join(
                                    f'<span class="unavailable-ref" id="{_id("ev-", ref)}">{_h(ref)}</span>' for ref in refs) + '</div></details>')
        if source_cards:
            cards.append(f'<details class="details-box"><summary>sysfs / 文件逐项来源（{len(source_cards)} 项，默认折叠）</summary>' +
                         "".join(source_cards) + '</details>')
        return "".join(cards)

    def commands_html(self):
        return "".join(f'<div class="evidence-item"><strong>{_h(command["id"])}</strong>'
                       f'<pre>{_h(_command_text(command))}</pre><p>stdout: {_raw_link(command.get("stdout_path"))} '
                       f'· stderr: {_raw_link(command.get("stderr_path"))}</p></div>' for command in self.commands) or _empty("未记录命令。")


def _device_details(topology, evidence):
    npu = {}
    for mapping in topology.get("npu_devices", []):
        npu.setdefault(str(mapping.get("bdf")), []).append(mapping)
    io = {}
    for mapping in topology.get("io_mapping", []):
        io.setdefault(str(mapping.get("bdf")), []).append(mapping)
    cards = []
    for bdf, device in sorted(topology.get("devices", {}).items()):
        link = device.get("link") or {}
        ceiling = layout.link_ceiling(link)
        fields = [("Kind / role", f"{device.get('kind', 'other')} / {device.get('role', 'other')}",
                   "inferred" if device.get("role_source") == "pci_class_inference" else "measured"),
                  ("实际 parent BDF", device.get("parent_bdf"), "measured"),
                  ("RootBus", device.get("root_bus"), "measured"),
                  ("CPU / NUMA 归属", f"Socket {device.get('cpu_socket')} / NUMA {device.get('numa_node')}",
                   "derived" if device.get("cpu_socket") is not None else "unknown"),
                  ("当前链路", layout.link_text(link), "measured" if link.get("current_speed_gt_s") is not None else "unknown"),
                  ("本端最大能力", layout.link_text(link, True), "measured" if link.get("max_speed_gt_s") is not None else "unknown"),
                  ("单向编码容量上界", f"{ceiling:.3f} GB/s（未扣协议开销）" if ceiling is not None else None, "derived"),
                  ("DSN 观测字段", device.get("dsn"), "measured"),
                  ("NPU 映射", npu.get(bdf), "measured" if all(item.get("mapping_verified") for item in npu.get(bdf, [])) else "unknown"),
                  ("I/O 单元映射", io.get(bdf), "measured" if all(item.get("confidence") == "observed" and item.get("unit_id") is not None
                                                               for item in io.get(bdf, [])) else
                   ("inferred" if any(item.get("confidence") == "inferred" for item in io.get(bdf, [])) else "unknown"))]
        refs = _list(device.get("evidence_refs"))
        for item in io.get(bdf, []):
            refs.extend(_list(item.get("evidence_refs")))
        cards.append(f'<details class="device-detail" id="{layout.node_id(bdf)}"><summary>{_h(bdf)} · '
                     f'{_h(device.get("kind", "other"))} · {_h(device.get("name"))}</summary><dl class="kv">' +
                     "".join(f'<dt>{_h(key)}</dt><dd>{_knowledge(kind if value is not None else "unknown")} {_h(value)}</dd>' for key, value, kind in fields) +
                     '</dl><p class="quiet">DSN 相同不证明同一物理 ASIC。未知 CPU / NUMA 归属不按 BDF 数值猜测。</p>' +
                     evidence.links(refs) + f'<details><summary>完整采集字段</summary><pre>{_h(_json(device))}</pre></details></details>')
    return "".join(cards) or _empty("没有 PCI 设备记录。")


def _cpu_context(topology):
    cpu = topology.get("cpu") or {}
    numa = topology.get("numa_nodes", [])
    sockets = set(node["socket"] for node in numa if node.get("socket") is not None)
    sockets.update(device["cpu_socket"] for device in topology.get("devices", {}).values() if device.get("cpu_socket") is not None)
    sockets.update(value for value in (cpu.get("socket_ids") or []) if value is not None)
    count = cpu.get("sockets")
    if type(count) is int and len(sockets) < count:
        sockets.add(None)  # A socket count does not establish its physical IDs.
    if not sockets:
        sockets = {None}
    cards = []
    for socket in sorted(sockets, key=lambda value: (value is None, str(value))):
        nodes = [node for node in numa if node.get("socket") == socket]
        units = [item for item in topology.get("io_mapping", []) if item.get("socket") == socket]
        unit_labels = sorted({f"{item.get('unit_kind', 'I/O')} {item.get('unit_id') if item.get('unit_id') is not None else '?'}"
                              + (" 候选 " + "/".join(map(str, item["unit_candidates"]))
                                 if item.get("unit_id") is None and item.get("unit_candidates") else "")
                              + f" ({item.get('confidence', 'unknown')}; {item.get('source', 'unknown')})" for item in units})
        cards.append('<div class="context-card">'
                     f'<strong>CPU Socket {_h(socket if socket is not None else "?")} · {_h(cpu.get("model_name"))}</strong>'
                     f'<p>{_h(cpu.get("vendor"))} · family {_h(cpu.get("family"))} / model {_h(cpu.get("model"))} · {_h(cpu.get("architecture"))}</p>'
                     f'<p>NUMA: {_h(", ".join(str(node.get("id")) + " [" + str(len(node.get("cpus", []))) + " logical CPUs]" for node in nodes) or "归属未采集")}</p>'
                     f'<p>I/O 映射: {_h("; ".join(unit_labels) or "未采集 / 不支持，不等于不存在")}</p></div>')
    unassigned = [node for node in numa if node.get("socket") is None]
    if unassigned and None not in sockets:
        cards.append('<div class="context-card"><strong>Socket 归属未知的 NUMA 节点</strong>' + _paragraph(unassigned) + '</div>')
    return ('<div class="context-grid">' + "".join(cards) + '</div><details class="details-box"><summary>CPU / NUMA 完整采集字段</summary>'
            f'<pre>{_h(_json(dict(cpu=cpu, numa_nodes=numa)))}</pre></details>')


BANDWIDTH_FIELDS = ["host", "case_id", "status", "validated", "memory", "mode", "backend", "cards", "rank", "bdf",
                    "physical_id", "mapping_verified", "direction", "card_GBs", "group_total_GBs", "group_H2D_GBs",
                    "group_D2H_GBs", "cpu_node", "memory_node", "raw_path", "receipt_path", "remarks"]


def bandwidth_rows(topology, results):
    """Long-form CSV rows; unsupported/unvalidated measurements stay null."""
    mapping = {str(item.get("logical_id")): item for item in topology.get("npu_devices", [])}
    rows = []
    for result in results:
        case, summary = result.get("case") or {}, result.get("summary") or {}
        status = result.get("status", "unknown")
        verified = status == "complete" and summary.get("verified") is True and result.get("validated", True) is not False
        case_id = str(case.get("id", summary.get("case_id", "unknown")))
        cards = case.get("cards", summary.get("cards", []))
        values = summary.get("card_GBs") or {}
        if isinstance(values, list):
            values = {str(card): value for card, value in zip(summary.get("card_ids", cards), values)}
        for rank in cards or [None]:
            item = mapping.get(str(rank), {})
            direction = case.get("directions", {}).get(str(rank))
            directions = summary.get("direction_GBs") or {}
            rows.append(dict(host=topology.get("host"), case_id=case_id,
                             status=status if status != "complete" or verified else "unvalidated", validated=verified,
                             memory=case.get("memory"), mode=case.get("mode"), backend=case.get("backend"),
                             cards=",".join(str(card) for card in cards), rank=rank, bdf=item.get("bdf"),
                             physical_id=item.get("physical_id"), mapping_verified=item.get("mapping_verified"),
                             direction=direction, card_GBs=_finite(values.get(str(rank))) if verified else None,
                             group_total_GBs=_finite(summary.get("total_GBs")) if verified else None,
                             group_H2D_GBs=_finite(directions.get("H2D")) if verified else None,
                             group_D2H_GBs=_finite(directions.get("D2H")) if verified else None,
                             cpu_node=case.get("cpu_node"), memory_node=case.get("memory_node"),
                             raw_path=result.get("raw_path"), receipt_path=result.get("receipt_path"),
                             remarks=(("HD 为 H2D+D2H 独立 buffer 合计字节 / 共同完成 wall；" +
                                       ("pinned 两条 device streams" if case.get("memory") == "pinned" else
                                        "pageable 两条 host 同步线程" if case.get("memory") == "pageable" else "copy 协议见 worker_meta"))
                                      if direction == "HD" else "") +
                             ("" if verified else "; 未完成或未通过验证，不用于带宽结论")))
    return rows


def _bandwidth_html(rows):
    if not rows:
        return _empty("尚无 DMA 结果。可以只采集拓扑；未测带宽不会显示为 0。")
    ranks = sorted({row["rank"] for row in rows if row["rank"] is not None}, key=lambda x: (isinstance(x, str), x))
    grouped = {}
    for row in rows:
        grouped.setdefault(row["case_id"], []).append(row)
    maximum = max((row["card_GBs"] for row in rows if row["card_GBs"] is not None), default=1) or 1
    table = ['<div class="table-wrap"><table><thead><tr><th>Case / 条件</th><th>状态</th><th>组总 GB/s</th>']
    table.extend(f'<th>NPU {_h(rank)} · GB/s</th>' for rank in ranks)
    table.append('<th>原始证据</th></tr></thead><tbody>')
    for case_id, group in grouped.items():
        first = group[0]
        by_rank = {row["rank"]: row for row in group}
        conditions = f'{first["mode"]} · {first["memory"]} · CPU NUMA={first["cpu_node"]} / Memory NUMA={first["memory_node"]}'
        table.append(f'<tr id="{_id("case-", case_id)}"><td class="wrap"><strong>{_h(case_id)}</strong><br><span class="quiet">{_h(conditions)}</span></td>'
                     f'<td>{_badge(first["status"])}</td><td class="num"><strong>{_fmt(first["group_total_GBs"])}</strong></td>')
        for rank in ranks:
            row = by_rank.get(rank, {})
            value = row.get("card_GBs")
            bar = f'<div class="meter"><span style="width:{max(0, min(100, value / maximum * 100)):.1f}%"></span></div>' if value is not None else ""
            table.append(f'<td class="num bar-cell">{_fmt(value)} <span class="quiet">{_h(row.get("direction", ""))}</span>{bar}</td>')
        table.append(f'<td>{_raw_link(first["raw_path"], "JSONL")} · {_raw_link(first["receipt_path"], "receipt")}</td></tr>')
    return "".join(table) + '</tbody></table></div><p class="fine-print">条形只比较本报告已验证的每卡实测值，不代表理论容量利用率。HD 为双向合计；无法从合计量猜测每个方向各占一半。</p>'


def _findings(analysis, evidence):
    cards, link_warnings = [], []
    items = sorted(analysis.get("findings", []), key=lambda item: (
        item.get("status") != "confirmed", item.get("status") != "supported"))
    for item in items:
        kind = _finding_kind(item)
        card = (f'<article class="finding" data-finding-id="{_h(item.get("id"))}" data-knowledge-kind="{kind}">'
                     f'<div class="finding-dimensions">{_knowledge(kind)}<span class="decision-label">实验判断 {_badge(item.get("status"))}</span></div>'
                     f'<h3>{_h(item.get("title", item.get("id")))}</h3>'
                     + _paragraph(item.get("claim")) + evidence.links(item.get("evidence_refs")) +
                     f'<div class="scope">适用范围：{_h(item.get("scope"))}<br>局限：{_h(item.get("limitations"))}</div></article>')
        (link_warnings if str(item.get("id", "")).startswith("link-") else cards).append(card)
    content = ('<div class="finding-grid">' + "".join(cards) + '</div>' if cards else
               _empty("尚无足够实验形成主要结论。拓扑事实与待检验的因果假设分别保留。"))
    if link_warnings:
        content += (f'<details class="details-box link-observations"><summary>次要链路状态观测 · {len(link_warnings)} 项（默认折叠，不能直接判为故障）</summary>'
                    '<p class="quiet">本端 current 低于 max 还可能来自对端能力、空端口或平台布线；这组观测不优先于本次 NPU 的匹配实验。</p>'
                    '<div class="finding-grid">' + "".join(link_warnings) + '</div></details>')
    return content


def _hypotheses(analysis, evidence):
    cards = []
    for item in analysis.get("hypotheses", []):
        experiments = _list(item.get("experiment_ids"))
        links = " ".join(f'<a href="#{_id("case-", value)}">{_h(value)}</a>' for value in experiments) or "尚未执行"
        steps = [("01 / 怀疑", item.get("suspicion")), ("02 / 若为真，应看到", item.get("prediction_if_true")),
                 ("03 / 若为假，应看到", item.get("prediction_if_false"))]
        body = "".join(f'<div class="chain-step"><div class="step-label">{_h(label)}</div>{_paragraph(value)}</div>' for label, value in steps)
        body += f'<div class="chain-step"><div class="step-label">04 / 匹配实验</div><p>{links}</p></div>'
        body += "".join(f'<div class="chain-step"><div class="step-label">{_h(label)}</div>{_paragraph(value)}</div>'
                        for label, value in [("05 / 实际观测", item.get("observed")), ("06 / 本轮判断", item.get("result"))])
        decision = item.get("result")
        decision_tag = _badge(decision) if isinstance(decision, str) and decision in STATUS else _badge("inconclusive")
        cards.append(f'<details class="hypothesis" open><summary>{_h(item.get("id"))} · {_h(item.get("suspicion"))}</summary>'
                     f'<div class="content"><div class="finding-dimensions">{_knowledge("inferred")}<span class="decision-label">实验判断 {decision_tag}</span></div><div class="chain">{body}</div>' +
                     evidence.links(item.get("evidence_refs")) + f'<div class="next-step"><strong>07 / 下一步</strong>{_paragraph(item.get("next_step"))}</div></div></details>')
    return "".join(cards)


def _proposals(analysis):
    proposals = analysis.get("proposals", [])
    if not proposals:
        return ""
    return '<h3>建议的后续实验 · 尚未自动执行</h3><div class="proposals">' + "".join(
        '<article class="proposal">' + _paragraph(proposal) + '</article>' for proposal in proposals) + '</div>'


def _comparisons(analysis, evidence):
    comparisons = analysis.get("comparisons", [])
    if not comparisons:
        return ""
    ratios = [item for item in comparisons if isinstance(item, dict) and "per_card_ratio" in item]
    table = ""
    if ratios:
        table = ('<p class="quiet">并发 / 匹配单卡带宽：100% 表示该卡保持单卡水平；缺少匹配单卡时不计算比值。</p>'
                 '<div class="table-wrap"><table><thead><tr><th>并发 Case</th><th>方向 / 内存</th><th>每卡相对单卡</th><th>缺失的单卡基线</th><th>证据</th></tr></thead><tbody>')
        for item in ratios:
            values = "; ".join(f'NPU {rank}: {_fmt(value * 100, 1)}%' if _finite(value) is not None else f'NPU {rank}: —'
                               for rank, value in item.get("per_card_ratio", {}).items())
            table += (f'<tr><td>{_h(item.get("case_id"))}</td><td>{_h(item.get("direction"))} / {_h(item.get("memory"))}</td>'
                      f'<td>{_h(values or "—")}</td><td>{_h(item.get("missing_solo") or "无")}</td><td>{evidence.links(item.get("evidence_refs"))}</td></tr>')
        table += '</tbody></table></div>'
    return '<h3>匹配对照与差异</h3>' + table + "".join(
        '<details class="details-box"><summary>' + _h(item.get("title", item.get("id", "实验对照"))) + '</summary>' +
        _paragraph({key: value for key, value in item.items() if key != "evidence_refs"}) + evidence.links(item.get("evidence_refs")) +
        '</details>' if isinstance(item, dict) else _paragraph(item) for item in comparisons if item not in ratios)


def _groups_html(groups):
    if not groups:
        return '<p class="fine-print">概览未折叠任何 PCI function。</p>'
    return '<details class="details-box"><summary>概览折叠组与精确成员（完整树始终保留全部 PCI functions）</summary>' + "".join(
        f'<article class="device-detail" id="{layout.node_id(group["bdf"])}"><strong>{_h(group["name"])}</strong>'
        f'<p class="quiet">RootBus {_h(group.get("root_bus"))} · 实际 parent {_h(group.get("parent_bdf"))} · 仅视觉聚合，不代表物理 ASIC</p><div class="membership">' +
        " ".join(f'<a href="#{layout.node_id(member)}">{_h(member)}</a>' for member in group["members"]) + '</div></article>' for group in groups) + '</details>'


def _focus_html(out, topology):
    focus = layout.focus_pages(topology)
    templates, options, pages = [], [], []
    for index, page in enumerate(focus["pages"]):
        filename = f'topology-focus-{page["id"]}.svg'
        (out / filename).write_text(page["svg"], encoding="utf-8")
        markup = _inline_svg(page["svg"], "focus-" + page["id"])
        templates.append(f'<template id="focus-template-{page["id"]}">{markup}</template>')
        counts = page["counts"]
        label = (f'RootBus {page["root_bus"]} · NPU {",".join(map(str, page["npu_ids"])) or "—"}'
                 f' · NIC {counts.get("nic", 0)} / NVMe {counts.get("nvme", 0)}')
        options.append(f'<option value="{index}">{_h(label)}</option>')
        pages.append({key: value for key, value in page.items() if key != "svg"})
        pages[-1]["path"] = filename
    info = {key: value for key, value in focus.items() if key != "pages"}
    info["pages"] = pages
    navigator = ('<div class="root-navigation" id="root-navigation"><span class="quiet">关键端点 / 实际祖先</span>'
                 '<button type="button" id="root-prev" aria-label="上一根总线">‹</button>'
                 '<select id="focus-root" aria-label="选择关键端点根总线">' + "".join(options) + '</select>'
                 '<button type="button" id="root-next" aria-label="下一根总线">›</button>'
                 '<span id="root-page-number" class="quiet"></span><a id="root-download" download>下载本页 SVG ↗</a></div>')
    omitted = ('<details class="details-box"><summary>关键端点视图省略的 PCI functions · '
               f'{len(focus["omitted_members"])} 项（完整树 / 精确概览 / 清单均保留）</summary>'
               '<p class="quiet">这里只减少默认图的视觉内容；不删除数据、不跨过祖先节点补连线。所有 NIC / NVMe functions 仍各自独立。</p>'
               '<div class="membership">' + " ".join(f'<a href="#{layout.node_id(bdf)}">{_h(bdf)}</a>' for bdf in focus["omitted_members"]) + '</div></details>')
    first = _inline_svg(focus["pages"][0]["svg"], "focus-initial-" + focus["pages"][0]["id"]) if focus["pages"] else ""
    return info, first, "".join(templates), navigator, omitted


def _timing_html(results):
    rows = []
    for result in results:
        summary, case = result.get("summary") or {}, result.get("case") or {}
        if result.get("status") != "complete" or summary.get("verified") is not True or result.get("validated", True) is False:
            continue
        for rank, values in (summary.get("timing_profile") or {}).items():
            if not isinstance(values, dict):
                continue
            row = [f'<td>{_h(case.get("id"))}</td><td>NPU {_h(rank)} · {_h(case.get("directions", {}).get(str(rank)))}</td>']
            for key in ("wall_s", "enqueue_wall_s", "final_sync_wall_s", "process_cpu_s", "h2d_event_ms", "d2h_event_ms"):
                value = _finite(values.get(key))
                value = value * 1000 if value is not None and key.endswith("_s") else value
                row.append(f'<td class="num">{_fmt(value, 3)}</td>')
            rows.append('<tr>' + "".join(row) + '</tr>')
    if not rows:
        return ""
    return ('<details class="details-box timing-observations" open><summary>提交 / 等待 / Event 时间 · 各重复轮的中位数（毫秒）</summary>'
            '<div class="finding-dimensions">' + _knowledge("measured") + _knowledge("derived") + '</div>'
            '<p class="quiet">测量时间来自 worker；表格为各字段独立中位数，不保证各列中位数严格相加。'
            'enqueue 是 copy API 内时间，同步 pageable API 可能包含传输完成，不能全部归为 CPU 开销。'
            'Event 是含 stream waits / host submission gaps 的 stream span，绝不是 DMA engine busy 时间。process CPU 为 worker 进程 CPU 时间。</p>'
            '<div class="table-wrap"><table><thead><tr><th>Case</th><th>设备 / 方向</th><th>Wall ms</th><th>Enqueue ms</th><th>Final sync ms</th>'
            '<th>Process CPU ms</th><th>H2D Event ms</th><th>D2H Event ms</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div></details>')


def _existing_artifact_link(out, value, label):
    safe = _safe_file(value)
    if safe:
        path = (out / safe).resolve()
        if path.is_file() and out.resolve() in path.parents:
            return _raw_link(safe, label)
    return f'<span class="quiet">{_h(label)}：未随本包提供 / 路径不可确认</span>'


def _pmu_html(out, results):
    """Display observed counters without treating rate/coverage as a ceiling."""
    def counter_status(status, observer=False):
        if status == "complete":
            return '<span class="pill complete">' + ("采集完成" if observer else "记录完整") + '</span>'
        return _badge(status)

    cases = []
    for result in results:
        pmu = result.get("pmu")
        if not isinstance(pmu, dict) or not pmu:
            continue
        case = result.get("case") or {}
        summary = pmu.get("summary") or {}
        metrics = [item for item in _list(summary.get("metrics") or summary.get("counters")) if isinstance(item, dict)]
        metrics.sort(key=lambda item: (_finite(item.get("whole_run_bytes")) is None,
                                      -(_finite(item.get("whole_run_bytes")) or 0), str(item.get("event", ""))))
        significant = [item for item in metrics if (_finite(item.get("whole_run_bytes")) or 0) > 1e9]
        case_status = result.get("status")
        if case_status == "complete" and (result.get("summary") or {}).get("verified") is not True:
            case_status = "unvalidated"
        content = [f'<details class="details-box pmu-case"><summary>PMU · {_h(case.get("id"))} · observer {_h(pmu.get("status"))}'
                   f' · {len(metrics)} 指标 / {len(significant)} 项 &gt;1 GB</summary>',
                   '<div class="finding-dimensions">' + _knowledge("measured") + _knowledge("derived") +
                   f'<span class="decision-label">DMA case {_badge(case_status)} · PMU observer {counter_status(pmu.get("status"), True)}</span></div>',
                   f'<p class="quiet">设备 {_h(case.get("cards"))} · 方向 {_h(case.get("directions"))} · {_h(case.get("memory"))} / {_h(case.get("backend"))}</p>',
                   '<p class="quiet">Whole-run 是整个 observer 窗口的累计量，包含 allocation / warmup / verification / background；'
                   '不等于各 timed-copy repeat 的精确计数。Peak 是 interval 观测速率，不是 IIO/IOM 硬件带宽上限。'
                   '不同 counter 可能重叠计数，不跨事件求和成“总带宽”。0 是有效零计数，— 是未知 / 不可用；部分覆盖不能当完整测量。</p>']
        if result.get("status") != "complete" or (result.get("summary") or {}).get("verified") is not True:
            content.append('<div class="callout">DMA case 未完成或未验证；此处计数只供运行诊断，不用于 DMA 带宽或因果结论。</div>')
        links = []
        if pmu.get("artifact_path_scope") == "run_directory":
            for key, label in (("raw_path", "原始 perf CSV"), ("stderr_path", "perf stderr"),
                               ("discovery_path", "PMU discovery"), ("receipt_path", "PMU receipt")):
                links.append(_existing_artifact_link(out, pmu.get(key), label))
        else:
            links.append('<span class="quiet">旧版 PMU 路径基准未声明；不猜测拼接路径，请参见 case receipt。</span>')
        links.append(_existing_artifact_link(out, result.get("receipt_path"), "case receipt"))
        content.append('<div class="pmu-links">' + ' · '.join(links) + '</div>')
        if metrics:
            content.append('<div class="table-wrap"><table class="pmu-table"><thead><tr><th>Socket / CPU</th><th>I/O 单元</th><th>Event / 方向语义</th>'
                           '<th>Whole-run GB</th><th>Running %</th><th>Peak GB/s</th><th>覆盖</th><th>状态</th><th>时间对齐</th></tr></thead><tbody>')
            for metric in metrics:
                count = _finite(metric.get("whole_run_bytes"))
                dominant = count is not None and count > 1e9
                covered = metric.get("coverage_complete")
                coverage = "完整" if covered is True else "不完整" if covered is False else "未知"
                significant_badge = ' <span class="pill supported">显著流量 &gt;1 GB</span>' if dominant else ""
                content.append(f'<tr class="{"pmu-dominant" if dominant else "pmu-other"}" data-pmu-event="{_h(metric.get("event"))}">'
                               f'<td>{_h(metric.get("socket"))} / {_h(metric.get("cpu"))}</td>'
                               f'<td>{_h(metric.get("unit_kind"))} {_h(metric.get("unit_id"))}</td>'
                               f'<td class="wrap"><strong>{_h(metric.get("event"))}</strong>'
                               f'{significant_badge}'
                               f'<br><span class="quiet">{_h(metric.get("direction_label", metric.get("direction")))}</span></td>'
                               f'<td class="num">{_fmt(count / 1e9 if count is not None else None, 6)}</td>'
                               f'<td class="num">{_fmt(metric.get("running_pct", metric.get("min_running_percent")), 2)}</td>'
                               f'<td class="num">{_fmt(metric.get("peak_GBs"), 3)}</td><td>{coverage}</td><td>{counter_status(metric.get("status"))}</td>'
                               f'<td class="wrap">{_h(metric.get("alignment"))}</td></tr>')
            content.append('</tbody></table></div>')
        else:
            content.append(_empty("没有可用 PMU 指标；缺失或不支持不会显示为零。"))
        if summary.get("limitations") or pmu.get("timing_uncertainty"):
            content.append('<details><summary>PMU 语义与时间边界</summary>' + _paragraph(
                dict(timing_uncertainty=pmu.get("timing_uncertainty"), limitations=summary.get("limitations"), errors=summary.get("errors"))) + '</details>')
        cases.append("".join(content) + '</details>')
    return '<h3>PMU / I/O 观察证据 · 每 case 可展开</h3>' + "".join(cases) if cases else ""


def _topology_markdown(topology, evidence, graphs):
    lines = ["# Topology evidence", "", "Every PCI function is distinct. RootBus is a logical root, not a shared wire. "
             "DSNs do not prove a shared physical ASIC. I/O associations preserve their recorded confidence/source.", "",
             f"Full graph: {graphs['full']['pci_nodes']} PCI nodes, {graphs['full']['pci_edges']} known parent edges.", "",
             "| BDF | Observed fact | Evidence refs |", "| --- | --- | --- |"]
    for bdf, device in sorted(topology.get("devices", {}).items()):
        fact = f"parent={device.get('parent_bdf')}; root={device.get('root_bus')}; kind={device.get('kind')}; role={device.get('role')}; "
        fact += f"socket={device.get('cpu_socket')}; NUMA={device.get('numa_node')}; "
        fact += f"current={layout.link_text(device.get('link') or {})}; max={layout.link_text(device.get('link') or {}, True)}"
        lines.append(f"| {_md(bdf)} | {_md(fact)} | {_md(device.get('evidence_refs') or 'not supplied')} |")
    lines.extend(["", "## I/O mappings", "", "These associations do not manufacture additional PCI wires.", ""])
    for mapping in topology.get("io_mapping", []):
        lines.append("- " + _md(mapping))
    lines.extend(["", "## Root-bus inventory", ""])
    for root in topology.get("roots", []):
        bus = root.get("root_bus")
        members = [bdf for bdf, device in topology.get("devices", {}).items() if device.get("root_bus") == bus]
        lines.append(f"- {_md(bus)}: socket={_md(root.get('cpu_socket'))}, NUMA={_md(root.get('numa_node'))}; exact members: " + ", ".join(members))
    lines.extend(["", "## Commands → raw output", ""])
    for command in evidence.commands:
        lines.extend([f"### {_md(command['id'])}", "", *_fence(_command_text(command), "text"), "",
                      f"returncode: {_md(command.get('returncode'))}; started_at: {_md(command.get('started_at'))}; duration_s: {_md(command.get('duration_s'))}", ""])
        for field in ("stdout_path", "stderr_path"):
            path = command.get(field)
            lines.append(f"- {field}: " + (f"[{_md(path)}]({quote(path, safe='/-._')})" if _safe_file(path) else _md(path)))
        lines.extend(["", *_fence(_preview(evidence.out, command.get("stdout_path")), "text"), ""])
    lines.extend(["## Missing / unresolved evidence refs", ""])
    unresolved = [key for key, value in evidence.entries.items() if not value.get("argv") and not value.get("source_file")]
    lines.extend(["- " + _md(ref) for ref in unresolved] or ["None recorded. Source-file existence is stated in report previews."])
    lines.extend(["", "## Exact sysfs read records", "",
                  "| Evidence ref | Read path | Offset / size | Status | Original value / bytes |",
                  "| --- | --- | --- | --- | --- |"])
    for ref, item in evidence.entries.items():
        record = item.get("read_record")
        if record:
            value = evidence.read_value(record)
            if len(value) > 2000:
                value = value[:2000] + " … preview truncated; full value in raw/sysfs-reads.json"
            lines.append(f"| {_md(ref)} | {_md(record.get('path'))} | {_md(record.get('offset'))} / {_md(record.get('size'))} | "
                         f"{_md(record.get('status'))} | {_md(value)} |")
    return "\n".join(lines) + "\n"


def _write_bandwidth(out, rows):
    with (out / "bandwidth.csv").open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=BANDWIDTH_FIELDS)
        writer.writeheader()
        writer.writerows({key: _csv_value(value) for key, value in row.items()} for row in rows)
    fields = ["case_id", "status", "memory", "rank", "bdf", "direction", "card_GBs", "group_total_GBs", "cpu_node", "memory_node"]
    lines = ["# DMA bandwidth", "", "Decimal GB/s. Blank/— means unavailable, never zero. HD counts H2D+D2H independent-buffer bytes over common completion wall time: "
             "pinned uses two device streams; pageable uses two synchronous host threads. "
             "Group total uses concurrent timing; it is not the sum of per-card medians.", "",
             "| " + " | ".join(fields) + " |", "| " + " | ".join("---" for _ in fields) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_fmt(row.get(key), 4) if key.endswith("GBs") else _md(row.get(key) if row.get(key) is not None else "—") for key in fields) + " |")
    (out / "bandwidth.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _reproduction_lines(out, topology, results):
    """Use the controller's sanitized cases and captured execution options."""
    keys = ("container", "env_script", "python", "performance", "case_timeout", "wait_idle_timeout", "idle_stable", "no_pmu")
    config_path = out / "execution-config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8")) if (
            config_path.is_file() and out.resolve() in config_path.resolve().parents) else {}
        config = {key: config[key] for key in keys if key in config}
    except (OSError, ValueError, TypeError):
        config = {}
    replay = ((out / "replay-source.json").is_file()
              or (topology.get("provenance") or {}).get("replay_is_new_measurement") is False)
    lines = ["## Preferred reproduction entry", "",
             "Run the archived controller entry from the run directory. It chooses the archived execution configuration "
             "and sanitized reproduction cases; keep the original evidence unchanged and choose a fresh output directory.", "",
             *_fence("./reproduce.sh reproduced-NEW", "bash"), ""]
    if replay:
        lines.extend(["This archive is a historical replay: ./reproduce.sh replays the recorded input and does not make new NPU measurements. "
                      "To measure another machine, run the skill's fresh collection/admission flow instead of reusing archived BDFs.", ""])
    else:
        lines.extend(["The archived topology is for reproducing on this same machine only; verify that runtime logical IDs still map "
                      "to the same physical BDFs. CPU policy changes and PMU collection retain the configured scope. "
                      "Admission still requires the selected host to be idle.", ""])
    if config:
        lines.extend(["### Recorded execution-config.json options", "", *_fence(_json(config), "json"), "",
                      "The single-case commands below retain container, CANN env-script, backend Python, performance policy, "
                      "timeouts, idle admission and PMU options. Environment paths are recorded; credentials are not.", ""])
    else:
        lines.extend(["No execution-config.json options are available; this report does not invent container, CANN or Python settings. "
                      "Prefer ./reproduce.sh; if it is absent, regenerate the archive using the skill's report command.", ""])
    options = []
    for key in keys[:-1]:
        if config.get(key) is not None:
            options.extend(["--" + key.replace("_", "-"), str(config[key])])
    if config.get("no_pmu"):
        options.append("--no-pmu")
    lines.extend(["## Exact benchmark reproduction cases", "",
                  "reproduce-cases/ is preferred: the controller removes stale fleet control and start-at fields before archiving. "
                  "Original cases/ and raw JSONL remain evidence, not mutable control files.", ""])
    for result in results:
        case = dict(result.get("case") or {})
        case.pop("control", None)
        case.pop("start_at_epoch_s", None)
        case_id = case.get("id")
        relative = None
        if isinstance(case_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", case_id):
            candidate = f"reproduce-cases/{case_id}.json"
            path = out / candidate
            if path.is_file() and out.resolve() in path.resolve().parents:
                try:
                    archived = json.loads(path.read_text(encoding="utf-8"))
                    if (isinstance(archived, dict) and archived.get("id") == case_id
                            and "control" not in archived and "start_at_epoch_s" not in archived):
                        relative, case = candidate, archived
                except (OSError, ValueError):
                    pass
        if relative and not replay:
            lines.extend(_fence(shlex.join(["python3", "reproduce-kit/dma.py", "run-case", "--topology", "topology.json",
                                           "--case", relative] + options + ["--out", f"reproduced-NEW/{case_id}"]), "bash") + [""])
        elif not replay:
            lines.extend(["The sanitized reproduce-cases file is not available for this case. Use ./reproduce.sh or regenerate the "
                          "archive; no direct command to an original fleet control case is suggested.", ""])
        lines.extend([*_fence(_json(case), "json"), ""])
    return lines


def render(out, topology, results, analysis, *, fleet=None):
    """Create offline report artifacts in ``out`` without modifying raw evidence.

    Returns paths keyed by ``report``, ``topology``, ``topology_overview``,
    ``bandwidth_csv``, ``bandwidth_md``, ``topology_evidence``, ``commands`` and
    ``metadata``. All input values are treated as untrusted display data.
    """
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    assets = _asset_dir()
    topology, results, analysis = dict(topology), list(results), dict(analysis)
    evidence = Evidence(out, topology, analysis)
    full_svg, full_meta = layout.svg(topology)
    overview_svg, overview_meta = layout.svg(topology, overview=True)
    machine_svg, machine_meta = layout.machine_svg(topology)
    graphs = {"full": full_meta, "overview": overview_meta, "machine": machine_meta}
    focus, focus_svg, focus_templates, root_navigator, focus_omitted = _focus_html(out, topology)
    (out / "topology.svg").write_text(full_svg, encoding="utf-8")
    (out / "topology-overview.svg").write_text(overview_svg, encoding="utf-8")
    (out / "topology-machine.svg").write_text(machine_svg, encoding="utf-8")
    rows = bandwidth_rows(topology, results)
    _write_bandwidth(out, rows)
    (out / "topology-evidence.md").write_text(_topology_markdown(topology, evidence, graphs), encoding="utf-8")
    command_lines = ["# Commands and reproduction", "", "The following argv were recorded by collection; this report did not execute them.", "",
                     "## New machine: collect fresh BDF and device mappings", "",
                     "Do not reuse an archived topology/BDF map on a different machine. Source that machine's CANN environment, "
                     "or configure the controller's --env-script path. Run this new-machine section from the installed skill directory. "
                     "Environment paths may be recorded; credentials must not be recorded.", "",
                     *_fence("python3 scripts/dma.py collect --out NEW_TOPOLOGY_RUN\n"
                             "python3 scripts/dma.py run --out NEW_RUN --cards all --memory pinned pageable "
                             "--directions H2D D2H HD --modes solo concurrent --backend torch --diagnose --performance require\n"
                             "python3 scripts/dma.py report --run NEW_RUN", "bash"), "", "## Recorded collection commands", ""]
    for command in evidence.commands:
        command_lines.extend([f"### {_md(command['id'])}", "", *_fence(_command_text(command), "bash"), ""])
    command_lines.extend(_reproduction_lines(out, topology, results))
    (out / "commands.md").write_text("\n".join(command_lines), encoding="utf-8")
    counts = {kind: sum(device.get("kind") == kind for device in topology.get("devices", {}).values()) for kind in ("npu", "nic", "nvme")}
    completed = sum(result.get("status") == "complete" and (result.get("summary") or {}).get("verified") is True
                    and result.get("validated", True) is not False for result in results)
    logical_ids = {str(item["logical_id"]) for item in topology.get("npu_devices", []) if item.get("logical_id") is not None}
    tested = {str(card) for result in results if result.get("status") == "complete"
              and (result.get("summary") or {}).get("verified") is True and result.get("validated", True) is not False
              for card in (result.get("case") or {}).get("cards", [])}
    untested = sorted(logical_ids - tested, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))
    metrics = [(str(full_meta["pci_nodes"]), "PCI functions · 完整树"), (str(len(topology.get("roots", []))), "Linux root buses"),
               (str(counts["npu"]), "NPU PCI functions"), (f"{counts['nic']} / {counts['nvme']}", "NIC / NVMe functions"),
               (f"{completed} / {len(results)}", "验证完成 / 已记录实验"),
               (f"{len(tested & logical_ids)} / {len(logical_ids)}" if logical_ids else "—", "有验证样本 / 已识别逻辑ID")]
    overview = '<div class="stats">' + "".join(f'<div class="stat"><div class="value">{_h(value)}</div><div class="label">{_h(label)}</div></div>' for value, label in metrics) + '</div>'
    provenance = topology.get("provenance") or {}
    if provenance.get("replay_is_new_measurement") is False:
        overview += ('<div class="callout"><strong>历史数据脱敏重放，非本次新实测。</strong> '
                     '保留原始采样的时序、字节和吞吐，以及脱敏 PCI 结构。原始命令 stdout/stderr 未公开，'
                     '公开证据范围为脱敏结构化记录；不能把这份示例当作新机器的验收结果。</div>')
    if untested:
        overview += ('<div class="coverage-note">' + _knowledge("unknown") + ' 尚无验证样本：NPU ' +
                     _h(", ".join(untested)) + '。已记录 case 完成不等于所有设备、内存类型和方向组合都通过验收。</div>')
    limitations = list(_list(analysis.get("limitations"))) + list(_list(topology.get("warnings"))) + full_meta["warnings"]
    limitations.extend(["PCIe 链路速率/宽度是采集时的端口状态；是否低于设计布线需核对对端能力与平台配置。",
                        "共享上游、同 RootBus 和相同 DSN 都不能单独证明带宽降低的原因。",
                        "IIO/IOM 编号依赖 CPU 型号、socket 与采集方法；未知映射保持未知，PMU 不支持不会显示为零。"])
    if completed != len(results):
        limitations.append(f"{len(results) - completed} 个实验未完成或未验证，已排除其数值；保留状态供复查。")
    if not topology.get("io_mapping"):
        limitations.append("没有可用的 NPU / PCI 设备 → IIO/IOM 单元映射。")
    limitations = list(dict.fromkeys(_text(value) for value in limitations))
    limitation_html = '<div class="incomplete"><strong>结论边界与缺失覆盖</strong><ul class="bullet-list">' + "".join(f'<li>{_h(value)}</li>' for value in limitations) + '</ul></div>'
    if analysis.get("coverage"):
        limitation_html += '<details class="details-box"><summary>完整实验覆盖记录</summary>' + _paragraph(analysis["coverage"]) + '</details>'
    if analysis.get("code_evidence"):
        limitation_html += '<details class="details-box"><summary>代码路径证据与适用版本</summary>' + _paragraph(analysis["code_evidence"]) + '</details>'
    search = []
    for bdf, device in topology.get("devices", {}).items():
        mappings = [m for m in topology.get("npu_devices", []) if m.get("bdf") == bdf]
        search.append(dict(bdf=bdf, text=" ".join([bdf, str(device.get("kind", "")).upper(), str(device.get("name", ""))] + [f"NPU {m.get('logical_id')}" for m in mappings])))
    fleet_html = '<h3>跨主机记录</h3>' + _paragraph(fleet) + '<p class="fine-print">跨主机不使用未同步的 monotonic 时钟合并带宽；各主机结果分别解释。</p>' if fleet else ""
    title = str(topology.get("host") or analysis.get("host") or "Ascend 主机")
    slots = dict(TITLE=_h(title), SUBTITLE=_h((topology.get("cpu") or {}).get("model_name") or "CPU 型号未采集"),
                 COLLECTED=_h(topology.get("collected_at")), CSS=(assets / "report.css").read_text(encoding="utf-8"),
                 JS=(assets / "report.js").read_text(encoding="utf-8"), OVERVIEW=overview + _knowledge_legend(), CPU_CONTEXT=_cpu_context(topology),
                 OVERVIEW_SVG=_inline_svg(overview_svg, "overview"), FULL_SVG=_inline_svg(full_svg, "full"),
                 MACHINE_SVG=_inline_svg(machine_svg, "machine"),
                 FOCUS_SVG=focus_svg, FOCUS_TEMPLATES=focus_templates, ROOT_NAVIGATOR=root_navigator,
                 GRAPH_STATUS=f'{full_meta["pci_nodes"]} PCI functions · 概览折叠 {overview_meta["grouped_pci_nodes"]} 个叶子函数',
                 GROUPS=focus_omitted + _groups_html(overview_meta["groups"]), PCI_COUNT=str(full_meta["pci_nodes"]),
                 DEVICES=_device_details(topology, evidence), BANDWIDTH='<div class="finding-dimensions">' + _knowledge("measured") + _knowledge("derived") + '</div>' + _bandwidth_html(rows) + _timing_html(results) + _pmu_html(out, results),
                 COMPARISONS=_comparisons(analysis, evidence), FLEET=fleet_html, FINDINGS=_findings(analysis, evidence),
                 HYPOTHESES=_hypotheses(analysis, evidence), PROPOSALS=_proposals(analysis), EVIDENCE=evidence.html(),
                 COMMANDS=evidence.commands_html(), LIMITATIONS=limitation_html,
                 DATA=_script_json(dict(graphs=graphs, focus=focus, search=search)))
    template = (assets / "report.html").read_text(encoding="utf-8")
    html = re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: slots[match[1]], template)
    (out / "report.html").write_text(html, encoding="utf-8")
    metadata = dict(schema_version="1.0", generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    host=topology.get("host"), graphs=graphs, focus=focus, result_statuses=[dict(case=(r.get("case") or {}).get("id"),
                    status=r.get("status"), raw_path=r.get("raw_path"), receipt_path=r.get("receipt_path")) for r in results],
                    input_sha256={"topology": sha256(_json(topology).encode()).hexdigest(),
                                  "results": sha256(_json(results).encode()).hexdigest(), "analysis": sha256(_json(analysis).encode()).hexdigest()},
                    rendering="Python standard library; all assets inline; no external network dependencies",
                    distinctions=dict(measured="PCI parents, port-reported links, copy-loop measurements and recorded hardware inventory",
                                      observed="Alias for measured structured observations; public replay scope is disclosed separately",
                                      derived="Gen1–5 encoded bandwidth ceiling; graph descendant membership",
                                      inferred="Only as explicitly labelled by analysis or io_mapping confidence",
                                      unknown="Not collected, unsupported, unverified or incomplete; never coerced to zero"))
    (out / "report-metadata.json").write_text(_json(metadata) + "\n", encoding="utf-8")
    return {"report": out / "report.html", "topology": out / "topology.svg", "topology_overview": out / "topology-overview.svg",
            "topology_machine": out / "topology-machine.svg",
            "bandwidth_csv": out / "bandwidth.csv", "bandwidth_md": out / "bandwidth.md", "topology_evidence": out / "topology-evidence.md",
            "commands": out / "commands.md", "metadata": out / "report-metadata.json"}
