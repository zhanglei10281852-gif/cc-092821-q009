from __future__ import annotations

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.germplasm.service import GermplasmService


def _source(service: GermplasmService, code: str, *, locality: str = "河谷试验站") -> dict:
    return service.accessions.create_source({
        "source_code": code, "provider_name": "合作站", "country_code": "CN",
        "locality": locality, "collected_on": "2025-10-02", "restrictions": {},
    })


def _accession(
    service: GermplasmService,
    number: str,
    source_id: int,
    *,
    scientific_name: str = "Oryza sativa",
    crop_name: str = "水稻",
    cultivar_name: str = "地方材料",
    passport: dict | None = None,
) -> dict:
    accession = service.accessions.create_accession({
        "accession_no": number, "scientific_name": scientific_name, "crop_name": crop_name,
        "cultivar_name": cultivar_name, "source_id": source_id, "acquisition_type": "采集",
        "received_on": "2026-09-01", "passport": passport or {}, "created_by": "登记员",
    })
    return service.accessions.transition(accession["id"], {
        "target_status": "accepted", "reason": "资料齐全", "expected_version": 1, "actor": "审核员",
    })


def _duplicate_pair(service: GermplasmService, tag: str = "D1") -> tuple[dict, dict]:
    source_a = _source(service, f"SRC-{tag}-A")
    source_b = _source(service, f"SRC-{tag}-B")
    first = _accession(
        service, f"ACC-{tag}-A", source_a["id"],
        passport={"collector": "旧系统", "latitude": 30.1, "restrictions": {"quota_kg": 5}},
    )
    second = _accession(
        service, f"ACC-{tag}-B", source_b["id"],
        cultivar_name="农家种",
        passport={"collector": "合作站", "altitude": 1200, "restrictions": {"no_distribution": True}},
    )
    return first, second


def _scan_one(service: GermplasmService) -> dict:
    result = service.duplicates.scan()
    assert result["created"] == 1, result
    return service.repository.require_candidate(result["candidate_ids"][0])


def _stored_lot(service: GermplasmService, accession: dict, tag: str) -> dict:
    location = service.inventory.create_location({
        "location_code": f"COLD-{tag}", "facility": "长期库", "room": "低温一室", "rack": "R1", "shelf": "S1",
        "capacity_grams": 2000, "temperature_c": -18, "humidity_percent": 30,
    })
    lot = service.inventory.create_lot({
        "lot_no": f"LOT-{tag}", "accession_id": accession["id"], "parent_lot_id": None,
        "harvest_year": 2025, "initial_weight_grams": 400, "moisture_percent": 7.5,
        "treatment": "清选干燥", "created_by": "登记员",
    })
    service.inventory.place_lot({
        "lot_id": lot["id"], "location_id": location["id"], "weight_grams": 400,
        "container_code": f"BOX-{tag}", "idempotency_key": f"place-{tag}-0001", "actor": "保管员",
    })
    return service.repository.lot_detail(lot["id"])


def _completed_test(service: GermplasmService, lot: dict, tag: str) -> dict:
    protocol = service.viability.create_protocol({
        "protocol_code": f"GER-{tag}", "crop_name": "水稻", "sample_size": 100, "replicate_count": 2,
        "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "根芽发育完整",
        "created_by": "技术负责人",
    })
    test = service.viability.schedule_test({
        "test_no": f"VT-{tag}", "lot_id": lot["id"], "protocol_id": protocol["id"], "test_type": "入库初检",
        "sampled_grams": 5, "scheduled_for": "2026-09-25", "requested_by": "检测员",
        "idempotency_key": f"schedule-{tag}-0001",
    })
    service.viability.start_test(test["id"], {"performed_by": "检测员", "expected_version": 1})
    for replicate, normal in [(1, 90), (2, 92)]:
        service.viability.add_count(test["id"], {
            "replicate_no": replicate, "seeds_tested": 100, "normal_count": normal,
            "abnormal_count": 5, "dead_count": 100 - normal - 5, "fresh_count": 0,
            "observation_day": 14, "observed_by": "检测员",
        })
    return service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})


def test_scan_generates_candidates_with_evidence_and_is_idempotent(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _duplicate_pair(service)
        far_source = _source(service, "SRC-FAR", locality="高原基地")
        _accession(service, "ACC-FAR", far_source["id"], scientific_name="Triticum aestivum", crop_name="小麦")
        result = service.duplicates.scan()
        assert result["ruleset_version"] == "dup-rules-v1"
        assert result["created"] == 1
        candidate = service.repository.require_candidate(result["candidate_ids"][0])
        assert 0.5 <= candidate["score"] <= 1
        assert candidate["ruleset_version"] == "dup-rules-v1"
        rules = {entry["rule"] for entry in candidate["evidence"]["rules"]}
        assert {"scientific_name_exact", "crop_name_exact", "source_locality_exact"} <= rules
        for entry in candidate["evidence"]["rules"]:
            assert "value_a" in entry and "value_b" in entry and entry["weight"] > 0
        again = service.duplicates.scan()
        assert again["created"] == 0 and again["unchanged"] == 1 and again["updated"] == 0
        assert service.repository.require_candidate(candidate["id"])["version"] == 1


def test_review_dismiss_defer_and_version_conflict(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _duplicate_pair(service, "R1")
        candidate = _scan_one(service)
        with pytest.raises(ValidationError):
            service.duplicates.review(candidate["id"], {
                "action": "dismiss", "expected_version": 1, "actor": "审核员", "note": "",
            })
        with pytest.raises(ConflictError):
            service.duplicates.review(candidate["id"], {
                "action": "defer", "expected_version": 99, "actor": "审核员", "note": "等待产地确认",
            })
        deferred = service.duplicates.review(candidate["id"], {
            "action": "defer", "expected_version": 1, "actor": "审核员", "note": "等待产地确认",
        })
        assert deferred["status"] == "deferred" and deferred["version"] == 2
        dismissed = service.duplicates.review(candidate["id"], {
            "action": "dismiss", "expected_version": 2, "actor": "审核员", "note": "确属不同材料",
        })
        assert dismissed["status"] == "dismissed"
        rescan = service.duplicates.scan()
        assert rescan["created"] == 0 and rescan["skipped"] == 1
        with pytest.raises(ConflictError):
            service.duplicates.review(candidate["id"], {
                "action": "defer", "expected_version": 3, "actor": "审核员", "note": "重复操作",
            })


def test_merge_combines_records_and_preserves_immutable_history(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        survivor, retired = _duplicate_pair(service, "M1")
        survivor_lot = _stored_lot(service, survivor, "M1-A")
        retired_lot = _stored_lot(service, retired, "M1-B")
        completed = _completed_test(service, retired_lot, "M1-B")
        request = service.distribution.create_request({
            "request_no": "DIST-M1", "requester": "作物研究所", "purpose": "区域试验",
            "items": [{"accession_id": retired["id"], "quantity_grams": 20}],
        })
        service.distribution.submit(request["id"], 1)
        service.distribution.decide(request["id"], {
            "approve": True, "expected_version": 2, "actor": "资源审核员", "reason": "材料充足",
        })
        candidate = _scan_one(service)
        preview = service.duplicates.preview_merge(candidate["id"], {
            "survivor_id": survivor["id"], "expected_version": 1, "field_decisions": {},
        })
        assert preview["dry_run"] is True
        assert preview["summary"]["lots_to_move"] == [{"id": retired_lot["id"], "lot_no": retired_lot["lot_no"]}]
        assert preview["summary"]["distribution_items_kept_immutable"] == 1
        assert service.repository.count_table("merge_records") == 0
        result = service.duplicates.merge(candidate["id"], {
            "survivor_id": survivor["id"], "expected_version": 1, "field_decisions": {},
            "reason": "同一地方品种重复建档", "actor": "数据管理员", "idempotency_key": "merge-m1-0000001",
        })
        assert result["replayed"] is False
        merge = result["merge"]
        # 保留档案归并了来源、护照、批次与限制
        kept = service.repository.require_accession(survivor["id"])
        assert kept["status"] == "accepted"
        assert kept["passport"]["altitude"] == 1200
        assert kept["passport"]["collector"] == "旧系统"
        assert kept["passport"]["restrictions"]["no_distribution"] is True
        assert kept["passport"]["restrictions"]["quota_kg"] == 5
        moved_lot = service.repository.require_lot(retired_lot["id"])
        assert moved_lot["accession_id"] == survivor["id"]
        assert service.repository.require_lot(survivor_lot["id"])["accession_id"] == survivor["id"]
        # 旧档案保留原值并指向保留档案
        gone = service.repository.require_accession(retired["id"])
        assert gone["status"] == "retired"
        assert gone["merged_into_id"] == survivor["id"]
        assert gone["merge_id"] == merge["id"]
        assert gone["cultivar_name"] == "农家种"
        # 已签发发放记录与检测结果保持不可变
        item = service.repository.distribution_detail(request["id"])["items"][0]
        assert item["accession_id"] == retired["id"]
        assert item["accession_no"] == retired["accession_no"]
        test_after = service.repository.require_test(completed["id"])
        assert test_after["lot_id"] == retired_lot["id"]
        assert test_after["germination_percent"] == completed["germination_percent"] == 91
        # 事件历史归并到保留档案，原档案历史不变
        survivor_events = service.repository.accession_detail(survivor["id"])["events"]
        copied = [e for e in survivor_events if (e["detail"] or {}).get("merged_from_accession_id") == retired["id"]]
        assert len(copied) == 2  # created + status_changed
        assert any(e["event_type"] == "merged_duplicate" for e in survivor_events)
        retired_events = service.repository.accession_detail(retired["id"])["events"]
        assert [e["event_type"] for e in retired_events] == ["created", "status_changed", "retired_as_duplicate"]
        # 别名与逐项字段决定
        alias_keys = {(a["alias_type"], a["alias_key"]) for a in merge["aliases"]}
        assert ("accession_no", retired["accession_no"]) in alias_keys
        assert ("passport", "collector") in alias_keys
        assert ("source", "SRC-M1-B") in alias_keys
        decisions = {d["field_name"]: d for d in merge["decisions"]}
        assert decisions["cultivar_name"]["decision"] == "keep_survivor"
        assert decisions["cultivar_name"]["retired_value"] == "农家种"
        assert decisions["cultivar_name"]["final_value"] == "地方材料"
        assert decisions["passport.collector"]["decision"] == "keep_survivor"
        assert decisions["passport.altitude"]["decision"] == "keep_retired"
        assert decisions["passport.restrictions"]["decision"] == "combined"
        assert decisions["passport.restrictions"]["final_value"]["no_distribution"] is True
        # 候选结案，引用图与审计理由完整
        assert service.repository.require_candidate(candidate["id"])["status"] == "merged"
        assert merge["reason"] == "同一地方品种重复建档"
        assert merge["before_graph"]["retired"]["lots"][0]["lot_no"] == retired_lot["lot_no"]
        assert merge["after_graph"]["retired"]["lots"] == []
        assert {lot["lot_no"] for lot in merge["after_graph"]["survivor"]["lots"]} == {
            survivor_lot["lot_no"], retired_lot["lot_no"],
        }
        assert merge["after_graph"]["survivor"]["merged_from"][0]["accession_no"] == retired["accession_no"]


def test_merge_is_idempotent_and_rejects_key_reuse(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        survivor, retired = _duplicate_pair(service, "I1")
        candidate = _scan_one(service)
        payload = {
            "survivor_id": survivor["id"], "expected_version": 1, "field_decisions": {},
            "reason": "重复建档", "actor": "数据管理员", "idempotency_key": "merge-i1-0000001",
        }
        first = service.duplicates.merge(candidate["id"], payload)
        second = service.duplicates.merge(candidate["id"], payload)
        assert second["replayed"] is True
        assert second["merge"]["id"] == first["merge"]["id"]
        assert service.repository.count_table("merge_records") == 1
        events = service.repository.accession_detail(survivor["id"])["events"]
        copied = [e for e in events if (e["detail"] or {}).get("merged_from_accession_id") == retired["id"]]
        assert len(copied) == 2
        with pytest.raises(ConflictError):
            service.duplicates.merge(candidate["id"], {**payload, "survivor_id": retired["id"]})


def test_merge_flattens_chains_and_blocks_closed_candidates(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        sources = [_source(service, f"SRC-C-{letter}") for letter in "ABC"]
        first = _accession(service, "ACC-C-A", sources[0]["id"])
        second = _accession(service, "ACC-C-B", sources[1]["id"])
        third = _accession(service, "ACC-C-C", sources[2]["id"])
        service.duplicates.scan()
        pair_ab = service.repository.candidate_by_key(f"dup:{first['id']}:{second['id']}")
        pair_bc = service.repository.candidate_by_key(f"dup:{second['id']}:{third['id']}")
        pair_ac = service.repository.candidate_by_key(f"dup:{first['id']}:{third['id']}")
        assert pair_ab and pair_bc and pair_ac
        service.duplicates.merge(pair_ab["id"], {
            "survivor_id": second["id"], "expected_version": 1, "field_decisions": {},
            "reason": "第一批合并", "actor": "数据管理员", "idempotency_key": "merge-c-ab-00001",
        })
        assert service.repository.require_candidate(pair_ac["id"])["status"] == "invalidated"
        with pytest.raises(ConflictError):
            service.duplicates.merge(pair_ac["id"], {
                "survivor_id": third["id"], "expected_version": 2, "field_decisions": {},
                "reason": "失效候选", "actor": "数据管理员", "idempotency_key": "merge-c-ac-00001",
            })
        with pytest.raises(ConflictError):
            service.duplicates.merge(pair_ab["id"], {
                "survivor_id": second["id"], "expected_version": 2, "field_decisions": {},
                "reason": "重复执行", "actor": "数据管理员", "idempotency_key": "merge-c-ab-00002",
            })
        service.duplicates.merge(pair_bc["id"], {
            "survivor_id": third["id"], "expected_version": 1, "field_decisions": {},
            "reason": "第二批合并", "actor": "数据管理员", "idempotency_key": "merge-c-bc-00001",
        })
        # 链式展平：A 直接指向最终保留档案 C
        flattened = service.repository.require_accession(first["id"])
        assert flattened["merged_into_id"] == third["id"]
        resolved = service.duplicates.resolve_number("ACC-C-A")
        assert resolved["merged"] is True
        assert len(resolved["chain"]) == 1
        assert resolved["resolved"]["id"] == third["id"]
        alias_targets = {
            row["alias_key"]
            for row in connection.execute(
                "SELECT alias_key FROM accession_aliases WHERE accession_id=? AND alias_type='accession_no'",
                (third["id"],),
            ).fetchall()
        }
        assert alias_targets == {"ACC-C-A", "ACC-C-B"}


def test_merge_validates_version_survivor_and_overrides(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        survivor, retired = _duplicate_pair(service, "V1")
        candidate = _scan_one(service)
        with pytest.raises(ConflictError):
            service.duplicates.merge(candidate["id"], {
                "survivor_id": survivor["id"], "expected_version": 7, "field_decisions": {},
                "reason": "版本过旧", "actor": "数据管理员", "idempotency_key": "merge-v1-0000001",
            })
        with pytest.raises(ValidationError):
            service.duplicates.merge(candidate["id"], {
                "survivor_id": 999999, "expected_version": 1, "field_decisions": {},
                "reason": "保留档案不在候选中", "actor": "数据管理员", "idempotency_key": "merge-v1-0000002",
            })
        with pytest.raises(ValidationError):
            service.duplicates.preview_merge(candidate["id"], {
                "survivor_id": survivor["id"], "expected_version": 1,
                "field_decisions": {"scientific_name": "retired"},
            })
        with pytest.raises(ValidationError):
            service.duplicates.preview_merge(candidate["id"], {
                "survivor_id": survivor["id"], "expected_version": 1,
                "field_decisions": {"cultivar_name": "sideways"},
            })


def test_field_decision_override_and_preview_matches_execution(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        survivor, retired = _duplicate_pair(service, "O1")
        candidate = _scan_one(service)
        overrides = {"cultivar_name": "retired", "passport.collector": "retired"}
        preview = service.duplicates.preview_merge(candidate["id"], {
            "survivor_id": survivor["id"], "expected_version": 1, "field_decisions": overrides,
        })
        assert service.repository.require_candidate(candidate["id"])["version"] == 1
        assert service.repository.count_table("merge_records") == 0
        result = service.duplicates.merge(candidate["id"], {
            "survivor_id": survivor["id"], "expected_version": 1, "field_decisions": overrides,
            "reason": "以合作站档案为准", "actor": "数据管理员", "idempotency_key": "merge-o1-0000001",
        })
        preview_decisions = {d["field"]: (d["decision"], d["final_value"]) for d in preview["decisions"]}
        executed = {d["field_name"]: (d["decision"], d["final_value"]) for d in result["merge"]["decisions"]}
        for field, expected in preview_decisions.items():
            assert executed[field] == expected
        kept = service.repository.require_accession(survivor["id"])
        assert kept["cultivar_name"] == "农家种"
        assert kept["passport"]["collector"] == "合作站"
        decisions = {d["field_name"]: d for d in result["merge"]["decisions"]}
        assert decisions["cultivar_name"]["decision"] == "keep_retired"
        assert decisions["cultivar_name"]["survivor_value"] == "地方材料"
        assert decisions["cultivar_name"]["final_value"] == "农家种"


def test_resolve_number_and_missing_number(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        survivor, retired = _duplicate_pair(service, "N1")
        candidate = _scan_one(service)
        service.duplicates.merge(candidate["id"], {
            "survivor_id": survivor["id"], "expected_version": 1, "field_decisions": {},
            "reason": "重复建档", "actor": "数据管理员", "idempotency_key": "merge-n1-0000001",
        })
        resolved = service.duplicates.resolve_number(retired["accession_no"])
        assert resolved["merged"] is True
        assert resolved["resolved"]["accession_no"] == survivor["accession_no"]
        direct = service.duplicates.resolve_number(survivor["accession_no"])
        assert direct["merged"] is False and direct["chain"] == []
        with pytest.raises(NotFoundError):
            service.duplicates.resolve_number("ACC-NOPE")


def test_init_db_backfills_merge_columns_on_legacy_database(tmp_path, monkeypatch):
    import sqlite3

    from app import database

    legacy = tmp_path / "legacy.db"
    connection = sqlite3.connect(legacy)
    connection.execute(
        "CREATE TABLE accessions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "accession_no TEXT NOT NULL UNIQUE,"
        "scientific_name TEXT NOT NULL,"
        "crop_name TEXT NOT NULL,"
        "cultivar_name TEXT NOT NULL DEFAULT '',"
        "source_id INTEGER,"
        "acquisition_type TEXT NOT NULL,"
        "received_on TEXT NOT NULL,"
        "status TEXT NOT NULL DEFAULT 'draft',"
        "quarantine_reason TEXT NOT NULL DEFAULT '',"
        "passport_json TEXT NOT NULL DEFAULT '{}',"
        "version INTEGER NOT NULL DEFAULT 1,"
        "created_by TEXT NOT NULL,"
        "created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL)"
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("GERMPLASM_DATABASE_PATH", str(legacy))
    database.close_connection()
    try:
        database.init_db()
        check = sqlite3.connect(legacy)
        columns = {row[1] for row in check.execute("PRAGMA table_info(accessions)").fetchall()}
        check.close()
        assert {"merged_into_id", "merge_id"} <= columns
    finally:
        database.close_connection()


def test_http_duplicate_merge_flow(client, admin):
    headers = admin["headers"]
    for suffix in ("H1A", "H1B"):
        source = client.post("/api/germplasm/sources", headers=headers, json={
            "source_code": f"SRC-{suffix}", "provider_name": "合作站", "country_code": "CN",
            "locality": "河谷试验站", "restrictions": {},
        })
        assert source.status_code == 201, source.text
        accession = client.post("/api/germplasm/accessions", headers=headers, json={
            "accession_no": f"ACC-{suffix}", "scientific_name": "Oryza sativa", "crop_name": "水稻",
            "cultivar_name": "地方材料", "source_id": source.json()["id"], "acquisition_type": "采集",
            "received_on": "2026-09-01", "passport": {}, "created_by": "登记员",
        })
        assert accession.status_code == 201, accession.text
        accepted = client.post(f"/api/germplasm/accessions/{accession.json()['id']}/transition", headers=headers, json={
            "target_status": "accepted", "reason": "资料齐全", "expected_version": 1, "actor": "审核员",
        })
        assert accepted.status_code == 200, accepted.text
    scan = client.post("/api/germplasm/duplicates/scan", headers=headers)
    assert scan.status_code == 200, scan.text
    assert scan.json()["created"] == 1
    listed = client.get("/api/germplasm/duplicates", headers=headers, params={"status": "pending"})
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    candidate_id = listed.json()["items"][0]["id"]
    detail = client.get(f"/api/germplasm/duplicates/{candidate_id}", headers=headers)
    assert detail.status_code == 200
    survivor_id = detail.json()["accession_a"]["id"]
    retired_no = detail.json()["accession_b"]["accession_no"]
    preview = client.post(f"/api/germplasm/duplicates/{candidate_id}/preview", headers=headers, json={
        "survivor_id": survivor_id, "expected_version": 1, "field_decisions": {},
    })
    assert preview.status_code == 200, preview.text
    assert preview.json()["dry_run"] is True
    merged = client.post(f"/api/germplasm/duplicates/{candidate_id}/merge", headers=headers, json={
        "survivor_id": survivor_id, "expected_version": 1, "field_decisions": {},
        "reason": "同一地方品种", "actor": "数据管理员", "idempotency_key": "http-merge-0001",
    })
    assert merged.status_code == 200, merged.text
    assert merged.json()["replayed"] is False
    merge_id = merged.json()["merge"]["id"]
    replay = client.post(f"/api/germplasm/duplicates/{candidate_id}/merge", headers=headers, json={
        "survivor_id": survivor_id, "expected_version": 2, "field_decisions": {},
        "reason": "同一地方品种", "actor": "数据管理员", "idempotency_key": "http-merge-0001",
    })
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    resolved = client.get(f"/api/germplasm/accessions/resolve/{retired_no}", headers=headers)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["merged"] is True
    assert resolved.json()["resolved"]["id"] == survivor_id
    record = client.get(f"/api/germplasm/merges/{merge_id}", headers=headers)
    assert record.status_code == 200, record.text
    body = record.json()
    assert body["reason"] == "同一地方品种"
    assert body["before_graph"]["retired"]["accession"]["accession_no"] == retired_no
    assert body["after_graph"]["survivor"]["accession"]["id"] == survivor_id
    assert body["decisions"] and body["aliases"]
    merges = client.get("/api/germplasm/merges", headers=headers)
    assert merges.status_code == 200 and merges.json()["total"] == 1


def test_http_duplicates_requires_authentication(client):
    assert client.get("/api/germplasm/duplicates").status_code == 401
    assert client.post("/api/germplasm/duplicates/scan").status_code == 401
