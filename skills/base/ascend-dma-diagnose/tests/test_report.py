"""Report tests cover graph fidelity, missing measurements and hostile evidence."""
import copy
import csv
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from dmalib import layout, report  # noqa: E402

NS = {"s": "http://www.w3.org/2000/svg"}


def device(bdf, parent=None, *, kind="bridge", role="bridge", root="0000:00", socket=0, numa=0, **extra):
    return dict(bdf=bdf, parent_bdf=parent, root_bus=root, kind=kind, role=role, name=kind.title(),
                cpu_socket=socket, numa_node=numa,
                link=dict(current_speed_gt_s=32, current_width=16, max_speed_gt_s=32, max_width=16),
                evidence_refs=["pci-command"], **extra)


def topology():
    devices = [device("0000:00:01.0", role="root_port"),
               device("0000:01:00.0", "0000:00:01.0", role="switch_upstream", dsn="same-dsn"),
               device("0000:02:00.0", "0000:01:00.0", role="switch_downstream", dsn="same-dsn"),
               device("0000:02:01.0", "0000:01:00.0", role="switch_downstream", dsn="same-dsn"),
               device("0000:03:00.0", "0000:02:00.0", kind="npu", role="endpoint"),
               device("0000:04:00.0", "0000:02:01.0", kind="npu", role="endpoint"),
               device("0000:05:00.0", "0000:02:00.0", kind="nic", role="endpoint", interfaces=["eth0"]),
               device("0000:06:00.0", "0000:02:01.0", kind="nvme", role="endpoint", model="Disk model")]
    devices.extend(device(f"0000:00:0{i}.0", kind="host", role="host") for i in range(5, 9))
    return dict(schema_version="1.0", host="example-host", collected_at="2026-11-25T00:00:00Z",
                cpu=dict(vendor="GenuineIntel", family=6, model=173, architecture="x86_64", model_name="Example CPU", sockets=1),
                numa_nodes=[dict(id=0, cpus=[0, 1, 2, 3], socket=0)],
                roots=[dict(root_bus="0000:00", cpu_socket=0, numa_node=0)],
                devices={d["bdf"]: d for d in devices},
                npu_devices=[dict(logical_id=i, physical_id=i+8, bdf=f"0000:0{i+3}:00.0",
                                  mapping_verified=True, mapping_source="explicit") for i in range(2)],
                io_mapping=[dict(bdf="0000:03:00.0", socket=0, unit_kind="IIO", unit_id=4, source="registers", confidence="observed")],
                commands=[dict(id="pci-command", argv=["lspci", "-Dtv"], returncode=0,
                               stdout_path="raw/pci.txt", stderr_path="raw/pci.err", started_at="now", duration_s=.1)],
                warnings=[])


def results():
    return [dict(case=dict(id="pair-H2D", cards=[0, 1], mode="concurrent", purpose="baseline", memory="pinned",
                           directions={"0": "H2D", "1": "H2D"}, backend="torch", cpu_node=0, memory_node=None),
                 status="complete", summary=dict(case_id="pair-H2D", verified=True, cards=[0, 1],
                                                   card_GBs={"0": 29.9, "1": 30.1}, total_GBs=59.8,
                                                   direction_GBs={"H2D": 59.8, "D2H": None}),
                 raw_path="results/pair-H2D.jsonl", receipt_path="receipts/pair-H2D.json")]


def analysis():
    return dict(host="example-host", findings=[dict(id="shared", title="Shared upstream observed", status="supported",
                claim="Visible shared path; causality unproven", scope="this host", limitations="needs contrast", evidence_refs=["pci-command"])],
                hypotheses=[dict(id="h1", suspicion="shared path limit", prediction_if_true="pair slower",
                                 prediction_if_false="pair unchanged", experiment_ids=["pair-H2D"], observed="29.9,30.1",
                                 result="inconclusive", next_step="test opposite directions", evidence_refs=["pci-command"])],
                proposals=[dict(id="mixed", cards=[0, 1], directions={"0": "H2D", "1": "D2H"})],
                comparisons=[dict(id="c1", observed_ratio=.55, evidence_refs=["pci-command"])], limitations=[])


def parse_svg(text):
    return ET.fromstring(text)


def pci_nodes(svg):
    return [node for node in svg.findall("s:a", NS) if "pci-node" in node.get("class", "").split()]


def edges(svg):
    return [node for node in svg.findall("s:g", NS) if "pci-edge" in node.get("class", "").split()]


def test_render_full_graph_is_bijective_with_functions_and_immediate_ancestry(tmp_path):
    top = topology()
    paths = report.render(tmp_path, top, results(), analysis())
    assert all(path.is_file() for path in paths.values())
    svg = parse_svg(paths["topology"].read_text())
    assert {node.get("data-bdf") for node in pci_nodes(svg)} == set(top["devices"])
    expected = {(d["parent_bdf"], bdf) for bdf, d in top["devices"].items() if d["parent_bdf"] in top["devices"]}
    assert {(edge.get("data-parent"), edge.get("data-child")) for edge in edges(svg)} == expected
    assert int(svg.get("data-pci-node-count")) == len(top["devices"])
    assert int(svg.get("data-pci-edge-count")) == len(expected)
    # DSN is an observed attribute, never a reason to merge bridge functions.
    assert len([node for node in pci_nodes(svg) if "same-dsn" in "".join(node.itertext())]) == 3
    assert "IIO 4" in "".join(svg.itertext())


def test_overview_exposes_exact_members_and_never_folds_npu_nic_nvme_or_bridges(tmp_path):
    top = topology()
    paths = report.render(tmp_path, top, [], {})
    full = parse_svg(paths["topology"].read_text())
    overview = parse_svg(paths["topology_overview"].read_text())
    metadata = json.loads(paths["metadata"].read_text())
    groups = metadata["graphs"]["overview"]["groups"]
    hidden = {bdf for group in groups for bdf in group["members"]}
    assert hidden == {f"0000:00:0{i}.0" for i in range(5, 9)}
    assert {node.get("data-bdf") for node in pci_nodes(overview)} | hidden == {node.get("data-bdf") for node in pci_nodes(full)}
    assert all(top["devices"][bdf]["kind"] in ("host", "other") for bdf in hidden)
    html = paths["report"].read_text()
    for bdf in hidden:
        assert f'href="#{layout.node_id(bdf)}"' in html


def test_shared_bus_is_not_shared_wire_and_bridge_internal_is_not_an_external_link():
    top = topology()
    svg, _ = layout.svg(top)
    actual = edges(parse_svg(svg))
    shared = [edge for edge in actual if "shared" in edge.get("class", "").split()]
    assert [(edge.get("data-parent"), edge.get("data-child")) for edge in shared] == [("0000:00:01.0", "0000:01:00.0")]
    internal = [edge for edge in actual if edge.get("data-parent") == "0000:01:00.0"]
    assert all(edge.get("data-relation") == "hierarchy" for edge in internal)
    # Two independent root ports on the same root bus gain no shared-wire edge.
    separate = topology()
    separate["devices"] = {d["bdf"]: d for d in [device("0000:00:01.0", role="root_port"),
                         device("0000:00:02.0", role="root_port"),
                         device("0000:03:00.0", "0000:00:01.0", kind="npu", role="endpoint"),
                         device("0000:04:00.0", "0000:00:02.0", kind="npu", role="endpoint")]}
    assert not [edge for edge in edges(parse_svg(layout.svg(separate)[0])) if "shared" in edge.get("class", "").split()]


def test_layout_supports_many_sockets_deep_switches_missing_parent_and_cycle():
    top = topology()
    top["cpu"]["sockets"] = 3
    top["devices"] = {}
    top["roots"] = []
    for socket in range(3):
        root = f"0000:{socket*64:02x}"
        top["roots"].append(dict(root_bus=root, cpu_socket=socket, numa_node=socket))
        parent = None
        for layer in range(11):
            bdf = f"0000:{socket*64+layer:02x}:01.0"
            top["devices"][bdf] = device(bdf, parent, root=root, socket=socket, numa=socket,
                                         role="endpoint" if layer == 10 else "bridge", kind="npu" if layer == 10 else "bridge")
            parent = bdf
    missing = device("0000:ff:01.0", "0000:ff:00.0", root="0000:00")
    top["devices"][missing["bdf"]] = missing
    top["devices"]["0000:fd:00.0"] = device("0000:fd:00.0", "0000:fe:00.0")
    top["devices"]["0000:fe:00.0"] = device("0000:fe:00.0", "0000:fd:00.0")
    svg, meta = layout.svg(top)
    assert len(pci_nodes(parse_svg(svg))) == len(top["devices"])
    assert len(edges(parse_svg(svg))) == 32
    assert meta["warnings"]
    assert meta["width"] > 4000
    # Boxes at the same depth cannot collide, even when labels vary in height.
    boxes = list(meta["boxes"].values())
    for i, first in enumerate(boxes):
        for second in boxes[i+1:]:
            if first["x"] == second["x"]:
                assert first["y"] + first["height"] <= second["y"] or second["y"] + second["height"] <= first["y"]


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.data_script, self.in_data = [], [], False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "script" and attrs.get("id") == "report-data":
            self.in_data = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_data = False

    def handle_data(self, data):
        if self.in_data:
            self.data_script.append(data)


def test_html_svg_and_json_script_escape_hostile_source_strings(tmp_path):
    top = topology()
    payload = '</script><script>alert("x")</script><img src=x onerror=alert(1)>&\u2028'
    top["host"] = payload
    top["devices"]["0000:03:00.0"]["name"] = payload
    top["commands"][0]["argv"].append(payload)
    top["devices"]["0000:03:00.0"]["evidence_refs"].append('javascript:alert(1)')
    result = results()
    result[0]["raw_path"] = 'javascript:alert(1)'
    a = analysis()
    a["findings"][0]["claim"] = payload
    paths = report.render(tmp_path, top, result, a)
    html = paths["report"].read_text()
    parser = Tags(); parser.feed(html)
    assert len([tag for tag, _ in parser.tags if tag == "script"]) == 2
    assert not [attrs for tag, attrs in parser.tags if tag == "img"]
    assert not [attrs for tag, attrs in parser.tags if "onerror" in attrs]
    assert not [attrs for _, attrs in parser.tags if str(attrs.get("href", "")).startswith("javascript:")]
    data = json.loads("".join(parser.data_script))
    assert payload in next(item["text"] for item in data["search"] if item["bdf"] == "0000:03:00.0")
    assert "\\u003c/script\\u003e" in "".join(parser.data_script)
    parse_svg(paths["topology"].read_text())
    assert not parse_svg(paths["topology"].read_text()).findall(".//s:script", NS)


@pytest.mark.parametrize("state,verified", [("unsupported", True), ("failed", True), ("busy", True), ("interrupted", True), ("complete", False)])
def test_incomplete_measurements_are_not_published_as_valid_numeric_bandwidth(tmp_path, state, verified):
    result = results()
    result[0]["status"] = state
    result[0]["summary"]["verified"] = verified
    paths = report.render(tmp_path, topology(), result, {})
    rows = list(csv.DictReader(paths["bandwidth_csv"].open()))
    assert len(rows) == 2
    assert all(row["card_GBs"] == row["group_total_GBs"] == "" for row in rows)
    assert all(row["status"] == ("unvalidated" if state == "complete" else state) for row in rows)


def test_csv_preserves_group_window_total_and_bdf_mapping_without_fabricating_hd_split(tmp_path):
    result = results()
    result[0]["case"]["directions"] = {"0": "HD", "1": "HD"}
    result[0]["case"]["id"] = "=HYPERLINK(1)"
    result[0]["summary"].pop("direction_GBs")
    paths = report.render(tmp_path, topology(), result, {})
    rows = list(csv.DictReader(paths["bandwidth_csv"].open()))
    assert rows[0]["case_id"].startswith("'=")
    assert rows[0]["group_total_GBs"] == "59.8"  # 29.9+30.1 is 60.0, a different statistic.
    assert rows[0]["physical_id"] == "8"
    assert rows[0]["bdf"] == "0000:03:00.0"
    assert rows[0]["group_H2D_GBs"] == rows[0]["group_D2H_GBs"] == ""


def test_evidence_resolves_fact_to_command_and_raw_output_without_reading_outside_root(tmp_path):
    top = topology()
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/pci.txt").write_text("raw output <observed-device>\n")
    top["commands"].append(dict(id="unsafe", argv=["echo", "example"], stdout_path="../secret.txt", returncode=0))
    paths = report.render(tmp_path, top, results(), analysis())
    html = paths["report"].read_text()
    markdown = paths["topology_evidence"].read_text()
    assert "raw output &lt;observed-device&gt;" in html
    assert "lspci -Dtv" in html
    assert "0000:03:00.0" in markdown and "parent=0000:02:00.0" in markdown
    assert "[raw/pci.txt](raw/pci.txt)" in markdown
    assert 'href="../secret.txt"' not in html
    assert "路径不可用" in html
    assert "./reproduce.sh" in paths["commands"].read_text()


def test_empty_topology_and_results_render_an_honest_offline_report(tmp_path):
    paths = report.render(tmp_path, dict(host="empty", devices={}), [], {}, fleet={"synchronized": False})
    html = paths["report"].read_text()
    assert "尚无 DMA 结果" in html
    assert "不支持不会显示为零" in html
    assert "跨主机不使用未同步" in html
    parser = Tags(); parser.feed(html)
    assert not [(tag, attrs) for tag, attrs in parser.tags if tag == "script" and "src" in attrs]
    assert not [(tag, attrs) for tag, attrs in parser.tags if tag == "link" and attrs.get("rel") == "stylesheet"]
    assert not pci_nodes(parse_svg(paths["topology"].read_text()))


def test_gen5_ceiling_is_derived_not_measured_and_unknown_encoding_stays_unknown():
    assert layout.link_ceiling(dict(current_speed_gt_s=32, current_width=16)) == pytest.approx(63.0153846)
    assert layout.link_ceiling(dict(current_speed_gt_s=64, current_width=16)) is None
    assert layout.link_ceiling(dict(current_speed_gt_s=None, current_width=16)) is None


def test_sysfs_fragment_evidence_resolves_the_exact_read_instead_of_a_literal_filename(tmp_path):
    top = topology()
    ref = "raw/sysfs-reads.json#read-000001"
    top["devices"]["0000:03:00.0"]["evidence_refs"].append(ref)
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/sysfs-reads.json").write_text(json.dumps([
        dict(id="read-000001", path="/sys/bus/pci/devices/0000:03:00.0/current_link_width",
             bytes_hex="31360a", status="complete", offset=None, size=None, sha256="recorded-hash")]))
    paths = report.render(tmp_path, top, [], {})
    html = paths["report"].read_text()
    md = paths["topology_evidence"].read_text()
    assert "只读来源" in html and "current_link_width" in html
    assert "recorded-hash" in html
    assert f"| {ref} |" in md and "current_link_width" in md
    assert "sysfs-reads.json%23read-000001" not in html


def test_replay_banner_and_quantitative_comparisons_are_visible(tmp_path):
    top = topology()
    top["provenance"] = dict(replay_is_new_measurement=False, evidence_scope="redacted_structured_only")
    a = analysis()
    a["comparisons"] = [dict(case_id="pair-H2D", direction="H2D", memory="pinned", per_card_ratio={"0": .5, "1": None}, missing_solo=[1])]
    paths = report.render(tmp_path, top, results(), a)
    html = paths["report"].read_text()
    assert "历史数据脱敏重放，非本次新实测" in html
    assert "NPU 0: 50.0%" in html and "NPU 1: —" in html


def test_ambiguous_iio_candidate_and_inferred_pci_role_are_labelled_explicitly(tmp_path):
    top = topology()
    top["io_mapping"][0].update(unit_id=None, unit_candidates=[0, 7], confidence="unknown")
    top["devices"]["0000:03:00.0"]["role_source"] = "pci_class_inference"
    paths = report.render(tmp_path, top, [], {})
    html = paths["report"].read_text()
    assert "IIO ? 候选 0/7" in html
    assert "IIO None" not in html
    assert "类别推定" in html


def test_commands_prefer_sanitized_reproduce_cases_and_keep_execution_config_options(tmp_path):
    result = results()
    result[0]["case"].update(control={"stop_file": "stale-fleet-control.json"}, start_at_epoch_s=42)
    archived = copy.deepcopy(result[0]["case"])
    archived.pop("control"); archived.pop("start_at_epoch_s")
    (tmp_path / "reproduce-cases").mkdir()
    (tmp_path / "reproduce-cases/pair-H2D.json").write_text(json.dumps(archived))
    (tmp_path / "execution-config.json").write_text(json.dumps(dict(container="worker container",
        env_script="/usr/local/Ascend/cann/set_env.sh", python="/opt/runtime/bin/python3", performance="require",
        case_timeout=400, wait_idle_timeout=900, idle_stable=15, no_pmu=True)))
    paths = report.render(tmp_path, topology(), result, {})
    commands = paths["commands"].read_text()
    assert "./reproduce.sh reproduced-NEW" in commands
    assert "--case reproduce-cases/pair-H2D.json" in commands
    assert "--case cases/" not in commands
    assert "--container 'worker container'" in commands
    assert "--env-script /usr/local/Ascend/cann/set_env.sh" in commands
    assert "--python /opt/runtime/bin/python3" in commands
    assert "--performance require" in commands
    assert "--case-timeout 400" in commands and "--wait-idle-timeout 900" in commands
    assert "--idle-stable 15" in commands and "--no-pmu" in commands
    assert "stale-fleet-control.json" not in commands
    assert '"start_at_epoch_s"' not in commands


def test_commands_for_historical_replay_do_not_suggest_new_hardware_case_execution(tmp_path):
    top = topology()
    top["provenance"] = dict(replay_is_new_measurement=False)
    (tmp_path / "reproduce-cases").mkdir()
    (tmp_path / "reproduce-cases/pair-H2D.json").write_text(json.dumps(results()[0]["case"]))
    commands = report.render(tmp_path, top, results(), {})["commands"].read_text()
    assert "./reproduce.sh reproduced-NEW" in commands
    assert "historical replay" in commands
    assert "reproduce-kit/dma.py run-case" not in commands


def test_focus_pages_keep_key_endpoints_and_ancestors_without_shortcut_edges():
    top = topology()
    top["devices"]["0000:80:01.0"] = device("0000:80:01.0", root="0000:80", role="root_port", socket=1)
    top["devices"]["0000:81:00.0"] = device("0000:81:00.0", "0000:80:01.0", root="0000:80", kind="npu", role="endpoint", socket=1)
    top["roots"].append(dict(root_bus="0000:80", cpu_socket=1, numa_node=1))
    top["npu_devices"].append(dict(logical_id=2, physical_id=2, bdf="0000:81:00.0", mapping_verified=True))
    focus = layout.focus_pages(top)
    assert len(focus["pages"]) == 2
    visible = {bdf for page in focus["pages"] for bdf in page["members"]}
    assert visible | set(focus["omitted_members"]) == set(top["devices"])
    assert not visible & set(focus["omitted_members"])
    critical = {bdf for bdf, value in top["devices"].items() if value["kind"] in ("npu", "nic", "nvme")}
    assert critical <= visible
    for page in focus["pages"]:
        svg = parse_svg(page["svg"])
        assert svg.get("data-view") == "focus"
        assert "关键端点局部页" in "".join(svg.itertext())
        for edge in edges(svg):
            assert top["devices"][edge.get("data-child")]["parent_bdf"] == edge.get("data-parent")
        assert {node.get("data-bdf") for node in pci_nodes(svg)} == set(page["members"])
        assert page["graph"]["height"] < 1800


def test_knowledge_type_is_separate_from_experiment_decision_and_link_warnings_collapse(tmp_path):
    a = analysis()
    a["findings"] = [dict(id="link-one", title="link warning", claim="below capability", status="supported", evidence_type="measured"),
                     dict(id="path-cause", title="shared-path main decision", claim="contrast", status="confirmed", evidence_type="inferred")]
    html = report.render(tmp_path, topology(), results(), a)["report"].read_text()
    assert html.index('data-finding-id="path-cause"') < html.index('data-finding-id="link-one"')
    parser = Tags(); parser.feed(html)
    warning = next(attrs for tag, attrs in parser.tags if tag == "details" and "link-observations" in attrs.get("class", ""))
    assert "open" not in warning
    main = next(attrs for tag, attrs in parser.tags if tag == "article" and attrs.get("data-finding-id") == "path-cause")
    assert main["data-knowledge-kind"] == "inferred"
    assert "实验判断" in html and "inferred 与 confirmed 可以并存" in html
    assert all(f'data-knowledge-kind="{kind}"' in html for kind in ("measured", "derived", "inferred", "unknown"))


def test_timing_profile_uses_milliseconds_and_preserves_stream_scope_and_unknowns(tmp_path):
    result = results()
    result[0]["summary"]["timing_profile"] = {"0": dict(wall_s=1., enqueue_wall_s=.001, final_sync_wall_s=.999,
                                                          process_cpu_s=.003, h2d_event_ms=900., d2h_event_ms=None)}
    html = report.render(tmp_path, topology(), result, {})["report"].read_text()
    assert "timing-observations" in html
    assert ">1,000.000</td>" in html and ">900.000</td>" in html
    assert "stream span" in html and "绝不是 DMA engine busy" in html
    assert "同步 pageable API 可能包含传输完成" in html
    result[0]["status"] = "failed"
    assert '<details class="details-box timing-observations"' not in report.render(tmp_path, topology(), result, {})["report"].read_text()


def test_missing_raw_source_refs_are_compacted_but_every_evidence_anchor_is_retained(tmp_path):
    top = topology()
    refs = [f"raw/sysfs-reads.json#read-{i:06d}" for i in range(20)]
    top["devices"]["0000:03:00.0"]["evidence_refs"].extend(refs)
    html = report.render(tmp_path, top, [], {})["report"].read_text()
    parser = Tags(); parser.feed(html)
    assert len([attrs for tag, attrs in parser.tags if tag == "details" and "missing-source" in attrs.get("class", "")]) == 1
    ids = {attrs.get("id") for _, attrs in parser.tags}
    assert all(report._id("ev-", ref) in ids for ref in refs)
    assert "20 项引用" in html and "原始文件未公开 / 未附带" in html


def test_completed_cases_do_not_mark_untested_logical_devices_as_passed(tmp_path):
    top = topology()
    top["npu_devices"].append(dict(logical_id=2, physical_id=2, bdf="0000:09:00.0", mapping_verified=False))
    html = report.render(tmp_path, top, results(), {})["report"].read_text()
    assert "2 / 3" in html
    assert "尚无验证样本：NPU 2" in html
    assert "不等于所有设备、内存类型和方向组合都通过验收" in html


def test_socket_count_does_not_invent_zero_based_physical_socket_ids(tmp_path):
    top = topology()
    top["cpu"].update(socket_ids=[3], sockets=1)
    top["numa_nodes"][0]["socket"] = 3
    for item in top["devices"].values():
        item["cpu_socket"] = 3
    for root in top["roots"]:
        root["cpu_socket"] = 3
    for mapping in top["io_mapping"]:
        mapping["socket"] = 3
    html = report.render(tmp_path, top, [], {})["report"].read_text()
    assert "CPU Socket 3" in html
    assert "CPU Socket 0" not in html


def pmu_result_fixture(tmp_path, scope=True):
    result = results()
    paths = dict(raw_path="pmu/observer.csv", stderr_path="pmu/observer.stderr",
                 discovery_path="pmu/discovery.json", receipt_path="pmu/observer.receipt.json")
    for value in list(paths.values()) + [result[0]["receipt_path"]]:
        path = tmp_path / value
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text("fixture")
    metrics = [dict(socket=0, cpu="CPU0", unit_kind="IOM", unit_id=0, event="iom0_zero", whole_run_bytes=0,
                    running_pct=100., peak_GBs=0., coverage_complete=True, status="complete", alignment="whole_observer_run_not_exact_DMA_repeats"),
               dict(socket=0, unit_kind="IOM", unit_id=1, event="iom1_unknown", whole_run_bytes=None, running_pct=None,
                    peak_GBs=None, coverage_complete=False, status="unavailable", alignment="unknown"),
               dict(socket=0, cpu="CPU0", unit_kind="IOM", unit_id=3, event="iom3_main", whole_run_bytes=2.5e9,
                    running_pct=100., peak_GBs=12., coverage_complete=True, status="complete", alignment="whole_observer_run_not_exact_DMA_repeats")]
    result[0]["pmu"] = dict(status="complete", summary=dict(metrics=metrics), **paths)
    if scope:
        result[0]["pmu"]["artifact_path_scope"] = "run_directory"
    return result


def test_pmu_table_keeps_valid_zero_unknown_and_significant_counts_distinct(tmp_path):
    result = pmu_result_fixture(tmp_path)
    html = report.render(tmp_path, topology(), result, {})["report"].read_text()
    parser = Tags(); parser.feed(html)
    rows = [attrs for tag, attrs in parser.tags if tag == "tr" and "data-pmu-event" in attrs]
    assert len(rows) == 3
    assert rows[0]["data-pmu-event"] == "iom3_main" and rows[0]["class"] == "pmu-dominant"
    zero = html.split('data-pmu-event="iom0_zero"', 1)[1].split('</tr>', 1)[0]
    unknown = html.split('data-pmu-event="iom1_unknown"', 1)[1].split('</tr>', 1)[0]
    assert ">0.000000</td>" in zero
    assert ">—</td>" in unknown and ">0.000000</td>" not in unknown
    assert "2.500000" in html and "100.00" in html
    assert "不是 IIO/IOM 硬件带宽上限" in html and "warmup / verification / background" in html
    assert all(f'href="{value}"' in html for value in result[0]["pmu"].values() if isinstance(value, str) and value.startswith("pmu/"))


def test_pmu_legacy_scope_does_not_guess_relative_file_links(tmp_path):
    result = pmu_result_fixture(tmp_path, scope=False)
    html = report.render(tmp_path, topology(), result, {})["report"].read_text()
    assert 'href="pmu/observer.csv"' not in html
    assert "旧版 PMU 路径基准未声明" in html
    assert f'href="{result[0]["receipt_path"]}"' in html
    result[0]["pmu"].update(artifact_path_scope="run_directory", raw_path="../../private.csv")
    html = report.render(tmp_path, topology(), result, {})["report"].read_text()
    assert 'href="../../private.csv"' not in html


def test_pmu_from_incomplete_dma_case_is_kept_as_diagnostic_only(tmp_path):
    result = pmu_result_fixture(tmp_path)
    result[0]["status"] = "interrupted"
    html = report.render(tmp_path, topology(), result, {})["report"].read_text()
    assert "不用于 DMA 带宽或因果结论" in html
    assert 'data-pmu-event="iom3_main"' in html


def test_pageable_hd_csv_and_markdown_do_not_claim_async_device_streams(tmp_path):
    result = results()
    result[0]["case"].update(memory="pageable", directions={"0": "HD", "1": "HD"})
    paths = report.render(tmp_path, topology(), result, {})
    rows = list(csv.DictReader(paths["bandwidth_csv"].open()))
    assert all("pageable 两条 host 同步线程" in row["remarks"] for row in rows)
    assert all("独立双流" not in row["remarks"] for row in rows)
    md = paths["bandwidth_md"].read_text()
    assert "pageable uses two synchronous host threads" in md
    assert "HD combines both independent streams" not in md
