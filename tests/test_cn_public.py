"""Synthetic public catalogues and lossless original-price extraction."""

import json
import csv
import io
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import pytest

from app.adapters._cn_public import GD, extract_table, original_amount, price_tables
from app.core.errors import DiscoveryError, SchemaChangedError
from app.core.http import HttpClient
from app.core.enums import PriceBasis, PriceStatus
from tests.test_adapters import make_adapter
from tests.test_cn_provincial import CODE, workbook


def gd_catalogue(http):
    for page in (1, 2):
        http.override(rf"paginateAnnouncement\?page={page}&", json.dumps({
            "data": {"page": page, "total": 4, "array": [
                {"id": str(i), "title": f"药品挂网价格通知{i}"} for i in range(page * 2 - 1, page * 2 + 1)
            ]},
        }).encode())
    for page, ids in ((1, ("a", "b")), (2, ("c",))):
        http.override(rf"medicines\?rows=50&currentPage={page}&", json.dumps({
            "currentPage": page, "totalCount": 3,
            "list": [{"id": i, "genericName": "测试片", "bidding_price": 0.3, "packing": "28片/盒"} for i in ids],
        }).encode())
    for i in range(1, 5):
        http.override(rf"detailAnnouncement\?id={i}$", json.dumps({
            "data": {"id": str(i), "title": "药品价格通知", "publishTime": "2020-01-02", "content": "<p>Public notice</p>",
                     "attachments": [{"url": "/attachement/test.xlsx", "fileName": "药品表"}] if i == 1 else []},
        }).encode())


def public_adapter(settings, http, registry, source_id="cn_gdmede"):
    adapter = make_adapter(source_id, settings, http, registry)
    adapter.config.options["internal_research_acknowledged"] = True
    return adapter


def test_full_catalogue_query_and_lossless_multi_price_extraction(settings, http, registry):
    adapter = public_adapter(settings, http, registry)
    gd_catalogue(http)
    columns = ["国家医保代码", "药品名称", "包装规格", "价格形成方式", "原包装挂网价格（元）", "包装挂网价格（元）"]
    http.override(r"/attachement/test.xlsx", workbook(columns, [
        dict(zip(columns, [CODE, "测试片", "28片/盒", 5, 40, 30])),
        dict(zip(columns, [CODE, "测试片", "28片/盒", 5, "待定", 29])),
    ]))
    snapshot = adapter.download(adapter.discover())
    dataset = adapter.parse(snapshot)
    normalized = adapter.normalize(dataset)
    coverage = dataset.context["coverage"]
    assert coverage["catalogue_total"] == 4 and coverage["public_query_rows"] == 3
    assert coverage["attachments_discovered"] == 1
    assert coverage["extracted_price_cells"] == 6 and coverage["unparsed_price_cells"] == 1
    assert coverage["normalizable_price_cells"] == 2
    # 2 established prices + 1 previous price + 3 public-query values, the last four with explicit warnings.
    assert coverage["published_price_cells"] == 6 and coverage["published_with_warnings_cells"] == 4
    facts = [fact for row in normalized.records for fact in row.prices]
    assert len(facts) == 6
    established = [f for f in facts if not f.quality_flags]
    assert sorted(f.amount for f in established) == [29, 30] and not any(f.never_current for f in established)
    previous = next(f for f in facts if "previous_value" in f.quality_flags)
    assert previous.amount == 40 and previous.never_current and previous.price_unit == "pack"
    assert previous.price_variant == "guawang_pack_listing:previous"
    assert previous.quality_flags == ("previous_value", "not_current_value")
    query = [f for f in facts if f.price_variant == "unclassified:bidding_price"]
    assert len(query) == 3 and all(f.price_type == PriceBasis.OTHER and f.price_unit == "unknown" for f in query)
    assert all(f.quality_flags == ("currency_inferred_cny", "price_basis_unknown", "price_concept_unknown",
                                   "publication_date_unknown") and not f.never_current for f in query)
    assert normalized.failed_rows == 0
    prices = [json.loads(line) for line in (Path(dataset.context["extraction_directory"]) / "prices.jsonl").read_text().splitlines()]
    assert not any(p["price_label"] == "价格形成方式" for p in prices)
    assert any(p["status"] == "previous" and p["amount"] == "40" for p in prices)
    assert any(p["amount"] is None and "Non-scalar" in p["reason"] for p in prices)
    assert all(not p["normalized"] for p in prices if p["price_label"] == "bidding_price")
    assert all("currentPage=" in p["source_url"] for p in prices if p["price_label"] == "bidding_price")
    assert len(snapshot.files_with_role_prefix("notice_")) == 4
    assert len(snapshot.files_with_role_prefix("query_")) == 2
    with (Path(dataset.context["extraction_directory"]) / "prices.csv").open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 7 and all(row["name"] == "测试片" for row in rows)
    assert any(row["code"] == CODE and row["amount"] == "30" for row in rows)
    unparsed = next(row for row in rows if not row["amount"])
    assert unparsed["published"] == "False" and unparsed["quality_flags"] == ""
    assert {row["quality_flags"] for row in rows if row["amount"] == "40"} == {"previous_value;not_current_value"}
    assert adapter.parse(snapshot).context["coverage"] == coverage


@pytest.mark.parametrize("kind", ["wrong_page", "duplicate", "changed_total", "empty"])
def test_pagination_refuses_incomplete_catalogue(kind, settings, http, registry):
    adapter = public_adapter(settings, http, registry)
    gd_catalogue(http)
    data = {"page": 2, "total": 4, "array": [{"id": "3", "title": "药品"}, {"id": "4", "title": "药品"}]}
    if kind == "wrong_page":
        data["page"] = 1
    elif kind == "duplicate":
        data["array"][0]["id"] = "1"
    elif kind == "changed_total":
        data["total"] = 5
    else:
        data["array"] = []
    http.override(r"paginateAnnouncement\?page=2&", json.dumps({"data": data}).encode())
    with pytest.raises(DiscoveryError):
        adapter.discover()


def test_hubei_entire_catalogue_and_access_failures_audited(settings, http, registry):
    adapter = public_adapter(settings, http, registry, "cn_hubei")
    http.override(r"zcwj.json$", json.dumps({"data": [
        {"URL": "http://ybj.hubei.gov.cn/drug.shtml", "FILENAME": "药品采购价格通知", "DOCRELTIME": "2020-01-02"},
        {"URL": "https://ybj.hubei.gov.cn/other.shtml", "FILENAME": "人员聘任通知"},
    ]}).encode())
    artifact = adapter.discover()
    manifest = json.loads(Path(urlparse(next(f.url for f in artifact.files if f.role == "public_manifest")).path).read_text())
    assert manifest["catalogue_total"] == 2 and manifest["notices_examined"] == 2
    assert len(manifest["failures"]) == 2 and len(manifest["excluded_notices"]) == 0
    assert artifact.partial


def test_hubei_browser_capture_is_snapshotted_without_challenge_bypass(settings, http, registry, tmp_path):
    adapter = public_adapter(settings, http, registry, "cn_hubei")
    item = {"URL": "http://ybj.hubei.gov.cn/drug.shtml", "FILENAME": "药品采购价格通知", "DOCRELTIME": "2020-01-02"}
    http.override(r"zcwj.json$", json.dumps({"data": [item]}).encode())
    archive = tmp_path / "capture.json"
    archive.write_text(json.dumps({"notices": [{
        "url": "https://ybj.hubei.gov.cn/drug.shtml", "status": 200,
        "html": '<a href="/prices.xlsx">药品价格附件</a>', "attachments": [],
    }]}))
    adapter.config.options["browser_notice_archive"] = str(archive)
    artifact = adapter.discover()
    assert any(f.role == "browser_notice_archive" for f in artifact.files)
    assert any(f.url == "https://ybj.hubei.gov.cn/prices.xlsx" for f in artifact.files)
    assert not any("/drug.shtml" in url for url in http.requested)


@pytest.mark.parametrize(("value", "expected"), [(12.3, "12.3"), (0.000001, "0.000001"), ("1,234.50元", "1234.50"), ("企业承诺10元", None), ("10-20", None), (True, None), ("待定", None), (None, None)])
def test_original_amount_does_not_invent_numeric_values(value, expected):
    assert original_amount(value)[0] == expected


@pytest.mark.parametrize("value", ["≤10元", "0.6元/片", "约12.3"])
def test_qualified_amounts_are_extracted_but_not_silently_normalized(value):
    amount, issue = original_amount(value)
    assert amount is not None and issue.startswith("Qualified original amount")


def test_caption_currency_and_pricing_units_keep_retail_separate_from_procurement(settings, http, registry):
    adapter = public_adapter(settings, http, registry)
    header = ["药品通用名", "计量单位", "最高零售价格"]
    records, prices = extract_table(header, [(5, ["测试片", "片", 2.5])], {
        "status": "final", "table_caption": "金额单位：元", "url": GD, "notice_url": GD, "published_at": "2020-01-02",
    }, "synthetic")
    assert prices[0]["currency"] == "CNY" and prices[0]["normalized"]
    record = adapter.normalize_public(records[0])
    assert record.prices[0].price_type == PriceBasis.RETAIL
    assert record.prices[0].price_status == PriceStatus.CEILING
    assert record.prices[0].price_unit == "tablet"
    assert record.product.ingredient is None


def test_pack_contents_do_not_establish_price_basis():
    _, prices = extract_table(["药品名称", "最小包装数量", "最小制剂单位", "最小包装单位", "挂网价（元）"],
                              [(2, ["测试片", 28, "片", "盒", 20])], {
        "status": "final", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert prices[0]["product"]["pack"] == "28片/盒"
    assert prices[0]["currency"] == "CNY" and prices[0]["fact_unit"] is None
    assert not prices[0]["normalized"]


def test_disclosure_identity_survives_attachment_reindexing():
    meta = {"status": "final", "url": GD + "/attachement/123.xlsx?filename=price.xlsx", "notice_url": GD}
    header = ["药品名称", "包装挂网价格（元）"]
    first, _ = extract_table(header, [(3, ["测试片", 20])], meta, "attachment_00001.xlsx:Sheet1")
    meta["url"] = GD + "/attachement/123.xlsx?filename=new-label.xlsx"
    second, _ = extract_table(header, [(3, ["测试片", 20])], meta, "attachment_00002.xlsx:Sheet1")
    assert first[0].key == second[0].key and first[0].row_ref != second[0].row_ref


@pytest.mark.parametrize("column", ["价格得分", "报价得分", "出厂价备案得分", "价格个得分", "报价企业", "价格单位", "最小计量单位（价格单位）"])
def test_price_scores_companies_and_units_are_not_price_cells(column):
    _, prices = extract_table(["药品名称", column, "挂网价"], [(2, ["测试片", 5, 20])], {
        "status": "final", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert len(prices) == 1 and prices[0]["price_label"] == "挂网价" and prices[0]["amount"] == "20"


def test_contract_total_is_kept_as_aggregate_not_unit_price():
    _, prices = extract_table(["药品名称", "单位", "协议采购金额（元）"], [(2, ["测试片", "盒", 200000])], {
        "status": "final", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert prices[0]["value_kind"] == "aggregate_amount" and not prices[0]["normalized"]
    assert prices[0]["fact_unit"] is None and prices[0]["price_type"] is None


def test_explicit_dosage_unit_price_is_not_divided_by_pack_size(settings, http, registry):
    adapter = public_adapter(settings, http, registry)
    records, prices = extract_table(["药品名称", "包装规格", "最小制剂单位", "最小制剂单位挂网价格（元）"],
                                   [(2, ["测试片", "28片/盒", "片", 2])], {
        "status": "final", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert prices[0]["normalized"] and prices[0]["fact_unit"] == "tablet"
    fact = adapter.normalize_public(records[0]).prices[0]
    from app.core.pricing import normalize_price

    assert normalize_price(fact)[0] == 2 and fact.pack_quantity == 28


def test_tender_benchmark_is_not_labeled_as_awarded_price():
    _, prices = extract_table(["药品名称", "单位", "增补中选价格基准（元）"], [(2, ["测试片", "片", 2])], {
        "status": "final", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert prices[0]["value_kind"] == "benchmark_price"
    assert prices[0]["price_variant"] == "tender_price_benchmark"


def test_training_manual_example_cannot_be_published_as_a_price():
    _, prices = extract_table(["药品名称", "包装挂网价格（元）"], [(2, ["测试片", 2])], {
        "status": "final", "label": "药品交易操作手册", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert prices[0]["status"] == "template" and not prices[0]["normalized"]


def test_nested_scanned_pdf_inventory_keeps_archive_path_and_page_numbers(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    @contextmanager
    def scanned_pdf(path):
        yield SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: "") for _ in range(3)])

    monkeypatch.setattr("pdfplumber.open", scanned_pdf)
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("scans/table.pdf", b"%PDF-test")
    path = tmp_path / "outer.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("wrapper.zip", inner.getvalue())
    exclusions = []
    assert list(price_tables(path, ".zip", exclusions=exclusions)) == []
    assert [item["page"] for item in exclusions] == [1, 2, 3]
    assert all(item["member"] == "wrapper.zip/scans/table.pdf" for item in exclusions)


@pytest.mark.parametrize("meta, header", [
    ({}, ["体外诊断试剂代码", "品种名称", "中选价格（元/盒）"]),
    ({"label": "医用耗材采购结果"}, ["产品名称", "包装挂网价格（元）"]),
])
def test_mixed_notices_do_not_turn_medical_devices_into_drug_prices(meta, header):
    records, prices = extract_table(header, [(2, ["CJ123", "检测试剂", 10])], {
        "status": "final", "url": GD, "notice_url": GD, **meta,
    }, "synthetic")
    assert records == prices == []


def test_packaging_measure_unit_is_not_inferred_from_the_prefix_of_its_label():
    _, prices = extract_table(["药品名称", "包装计量单位", "中选价格（元/包装计量单位）"],
                             [(2, ["测试片", "片", 2])], {
        "status": "final", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert prices[0]["pricing_unit"] == "片" and prices[0]["fact_unit"] == "tablet"


def test_minimum_unit_does_not_inherit_the_outer_pack_divisor():
    _, prices = extract_table(["药品名称", "包装规格", "最小制剂单位", "最小制剂单位挂网价格（元）"],
                             [(2, ["测试制剂", "6盒/箱", "盒", 2])], {
        "status": "final", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert prices[0]["fact_unit"] is None and not prices[0]["normalized"]


@pytest.mark.parametrize("column", ["医保统一编码", "药品统一编码", "医保代码", "医保药品分类与代码", "国家药品代码", "价格治理医保编码"])
def test_observed_national_code_aliases_preserve_identifiers_and_pdf_wrapping(column):
    wrapped = CODE[:9] + "\n" + CODE[9:]
    records, prices = extract_table([column, "药品名称", "包装挂网价格（元）"], [(2, [wrapped, "测试片", 2])], {
        "status": "final", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert records[0].key == prices[0]["product"]["code"] == CODE
    assert prices[0]["raw"][f"1:{column}"] == wrapped


def test_explicit_price_measure_column_is_not_itself_a_price():
    _, prices = extract_table(["药品名称", "最小计量单位（价格单位）", "中选价格（元）"],
                             [(2, ["测试片", "片", 2])], {
        "status": "final", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert len(prices) == 1 and prices[0]["fact_unit"] == "tablet" and prices[0]["normalized"]


@pytest.mark.parametrize("column", ["入市价", "同类品种最低价", "同类品种中位价", "同类品种最高价", "全国最低价"])
def test_observed_additional_price_labels_are_extracted(column):
    _, prices = extract_table(["药品名称", column], [(2, ["测试片", 2])], {
        "status": "final", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert len(prices) == 1 and prices[0]["amount"] == "2"


@pytest.mark.parametrize("column", ["同采购组日均费用均值（元）", "最高日均费用/最高有效申报价（元）"])
def test_daily_cost_benchmarks_never_become_per_unit_prices(column):
    _, prices = extract_table(["药品名称", "计价单位", column], [(2, ["测试片", "片", 2])], {
        "status": "final", "url": GD, "notice_url": GD,
    }, "synthetic")
    assert prices[0]["value_kind"] == "daily_cost" and not prices[0]["normalized"]
    assert prices[0]["fact_unit"] is None


def test_notice_titles_do_not_limit_drug_catalogue_discovery(settings, http, registry):
    adapter = public_adapter(settings, http, registry)
    gd_catalogue(http)
    http.override(r"paginateAnnouncement\?page=1&", json.dumps({
        "data": {"page": 1, "total": 4, "array": [
            {"id": "1", "title": "关于公示201608交易品种入市价的通知"},
            {"id": "2", "title": "关于公布国采接续采购中选结果的通知"},
        ]},
    }).encode())
    artifact = adapter.discover()
    assert len([f for f in artifact.files if f.role.startswith("notice_")]) == 4
    manifest = json.loads(Path(urlparse(next(f.url for f in artifact.files if f.role == "public_manifest")).path).read_text())
    assert manifest["excluded_notices"] == []


def test_generic_non_drug_products_are_not_medicine_prices():
    assert extract_table(["产品名称", "单位", "中选价格（元）"], [(2, ["人工晶体", "盒", 200])], {
        "status": "final", "url": "https://ybj.hubei.gov.cn/example.xlsx", "notice_url": "https://ybj.hubei.gov.cn/",
        "title": "关于执行中选结果的通知",
    }, "synthetic") == ([], [])


def test_non_drug_attachment_is_inventoried_without_downloading_large_device_catalogues(settings, http, registry):
    adapter = public_adapter(settings, http, registry)
    gd_catalogue(http)
    http.override(r"detailAnnouncement\?id=1$", json.dumps({
        "data": {"id": "1", "title": "药品及医用耗材采购通知", "publishTime": "2020-01-02", "content": "",
                 "attachments": [{"url": "/attachement/devices.xlsx", "fileName": "医用耗材目录"}]},
    }).encode())
    artifact = adapter.discover()
    assert not any(f.role.startswith("attachment_") for f in artifact.files)
    manifest = json.loads(Path(urlparse(next(f.url for f in artifact.files if f.role == "public_manifest")).path).read_text())
    assert manifest["attachments"][0]["excluded_reason"] == "non-drug attachment"


@pytest.mark.parametrize("caption", ["废止的药品价格", "撤网药品名单"])
def test_explicitly_withdrawn_table_prices_are_retained_not_promoted(caption):
    _, prices = extract_table(["药品名称", "包装挂网价格（元）"], [(3, ["测试片", 20])], {
        "status": "final", "table_caption": caption, "url": GD, "notice_url": GD,
    }, "synthetic")
    assert prices[0]["amount"] == "20" and prices[0]["status"] == "withdrawn" and not prices[0]["normalized"]


def test_verified_snapshot_can_be_reparsed_without_network(settings, http, registry):
    adapter = public_adapter(settings, http, registry)
    gd_catalogue(http)
    http.override(r"/attachement/test.xlsx", workbook(["药品名称", "包装挂网价格（元）"],
                                                   [{"药品名称": "测试片", "包装挂网价格（元）": 30}]))
    snapshot = adapter.download(adapter.discover())
    adapter.config.options["public_snapshot_dir"] = str(snapshot.directory)
    http.requested.clear()
    replay = adapter.download(adapter.discover())
    assert replay.id == snapshot.id
    assert adapter.parse(replay).context["coverage"]["extracted_price_cells"] == 4
    assert not http.requested


@pytest.mark.parametrize("suffix", [".xlsx", ".pdf"])
def test_one_broken_document_does_not_discard_successful_attachments(suffix, settings, http, registry):
    adapter = public_adapter(settings, http, registry)
    gd_catalogue(http)
    http.override(r"detailAnnouncement\?id=1$", json.dumps({
        "data": {"id": "1", "title": "药品价格通知", "publishTime": "2020-01-02", "content": "<p>Public notice</p>",
                 "attachments": [{"url": "/attachement/test" + suffix}]},
    }).encode())
    http.override(r"/attachement/test", b"not a valid document")
    snapshot = adapter.download(adapter.discover())
    parsed = adapter.parse(snapshot)
    assert parsed.context["coverage"]["extracted_price_cells"] == 3
    assert len(parsed.context["coverage"]["exclusions"]) == 1


def test_categorical_price_method_does_not_inflate_extraction(settings, http, registry):
    adapter = public_adapter(settings, http, registry)
    gd_catalogue(http)
    http.override(r"/attachement/test.xlsx", workbook(["药品名称", "价格形成方式"], [{"药品名称": "测试片", "价格形成方式": 5}]))
    parsed = adapter.parse(adapter.download(adapter.discover()))
    assert parsed.context["coverage"]["extracted_price_cells"] == 3
    assert parsed.context["coverage"]["normalizable_price_cells"] == 0

def test_duplicate_headers_and_proposals_retained_without_normalization():
    header = ["药品名称", "包装挂网价格（元）", "包装挂网价格（元）", "价格形成方式"]
    records, prices = extract_table(header, [(2, ["测试片", 2, 3, 5])],
                                   {"status": "proposed", "url": GD, "notice_url": GD}, "synthetic")
    assert len(records) == len(prices) == 2
    assert {p["amount"] for p in prices} == {"2", "3"}
    assert len(prices[0]["raw"]) == 4
    assert all(not p["normalized"] for p in prices)


def test_zip_csv_and_docx_tables_are_extractable(tmp_path):
    path = tmp_path / "prices.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("prices.csv", "药品名称,挂网价\n测试片,12\n")
    tables = list(price_tables(path, ".zip"))
    assert len(tables) == 1 and tables[0][0] == "prices.csv/csv"
    docx = tmp_path / "prices.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:tbl><w:tr><w:tc><w:p><w:r><w:t>药品名称</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>挂网价</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>测试片</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:document>')
    assert list(price_tables(docx, ".docx"))[0][1] == ["药品名称", "挂网价"]
    mislabeled = tmp_path / "mislabeled.doc"
    mislabeled.write_bytes(docx.read_bytes())
    assert list(price_tables(mislabeled, ".doc"))[0][1] == ["药品名称", "挂网价"]


def test_xlsx_bytes_with_legacy_xls_extension_are_not_discarded(tmp_path):
    path = tmp_path / "prices.xls"
    path.write_bytes(workbook(["药品名称", "包装挂网价格（元）"],
                              [{"药品名称": "测试片", "包装挂网价格（元）": 20}]))
    tables = list(price_tables(path, ".xls"))
    assert len(tables) == 1 and len(tables[0].rows) == 1


def test_native_archive_reader_streams_members_without_using_their_paths(tmp_path):
    path = tmp_path / "synthetic.rar"
    content = "药品名称,挂网价\n测试片,12\n".encode()
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo("folder/prices.csv")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    tables = list(price_tables(path, ".rar"))
    assert len(tables) == 1 and tables[0].name == "folder/prices.csv/csv"
    assert not (tmp_path / "folder").exists()


def test_unsafe_archive_is_not_extracted(tmp_path):
    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../escaped.csv", "药品名称,挂网价\n测试片,12")
    with pytest.raises(SchemaChangedError, match="Unsafe archive"):
        list(price_tables(path, ".zip"))
    assert not (tmp_path.parent / "escaped.csv").exists()


def test_shared_http_throttle_preserves_delay_for_parallel_requests(monkeypatch):
    now = [0.0]
    starts = []
    monkeypatch.setattr("app.core.http.time.monotonic", lambda: now[0])

    def sleep(delay):
        now[0] += delay

    monkeypatch.setattr("app.core.http.time.sleep", sleep)
    http = HttpClient(user_agent="test", delay_seconds=0.5)
    try:
        throttle_locked = http._throttle_locked

        def record_start(url):
            throttle_locked(url)
            starts.append(now[0])

        monkeypatch.setattr(http, "_throttle_locked", record_start)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: http._throttle(GD), range(8)))
        assert starts == [i * 0.5 for i in range(8)]
    finally:
        http.close()
