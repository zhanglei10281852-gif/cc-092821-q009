from __future__ import annotations


def _create_duplicate_pair(client, headers: dict, suffix: str = "1") -> dict:
    source_a = client.post("/api/germplasm/sources", headers=headers, json={
        "source_code": f"API-OLD-{suffix}", "provider_name": "旧系统", "country_code": "CN",
        "locality": "贵州从江加榜", "collected_on": "2018-10-01", "restrictions": {},
    })
    assert source_a.status_code == 201, source_a.text
    source_b = client.post("/api/germplasm/sources", headers=headers, json={
        "source_code": f"API-STA-{suffix}", "provider_name": "合作站", "country_code": "CN",
        "locality": "贵州从江加榜", "collected_on": "2018-10-01", "restrictions": {},
    })
    assert source_b.status_code == 201, source_b.text
    records = []
    for number, source_id, origin in (
        (f"API-OLD-{suffix}", source_a.json()["id"], f"OLD{suffix}"),
        (f"API-STA-{suffix}", source_b.json()["id"], f"STA{suffix}"),
    ):
        response = client.post("/api/germplasm/accessions", headers=headers, json={
            "accession_no": number, "scientific_name": "Oryza sativa", "crop_name": "水稻",
            "cultivar_name": "加榜香禾", "source_id": source_id,
            "acquisition_type": "采集" if origin.startswith("OLD") else "交换",
            "received_on": "2019-03-01",
            "passport": {"locality": "贵州从江加榜", "collector": "李四", "origin_code": origin},
            "created_by": "登记员",
        })
        assert response.status_code == 201, response.text
        accepted = client.post(f"/api/germplasm/accessions/{response.json()['id']}/transition", headers=headers, json={
            "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
        })
        assert accepted.status_code == 200, accepted.text
        records.append(accepted.json())
    return {"left": records[0], "right": records[1]}


def test_duplicate_merge_full_http_flow(client, admin):
    headers = admin["headers"]
    pair = _create_duplicate_pair(client, headers)

    scan = client.post("/api/germplasm/duplicates/scan", headers=headers, json={"actor": "数据管理员"})
    assert scan.status_code == 200, scan.text
    assert scan.json()["created"] >= 1
    assert scan.json()["rule_version"] == "duplicate-rules-v1"

    listing = client.get("/api/germplasm/duplicates/candidates?status=open", headers=headers)
    assert listing.status_code == 200
    candidate = next(
        item for item in listing.json()
        if {item["left_accession_id"], item["right_accession_id"]} == {pair["left"]["id"], pair["right"]["id"]}
    )
    evidence_fields = {item["field"] for item in candidate["evidence"]}
    assert {"scientific_name", "source.locality", "passport.locality"} <= evidence_fields

    detail = client.get(f"/api/germplasm/duplicates/candidates/{candidate['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["left_accession"]["accession_no"]

    # 暂缓后重开，验证人工判定流转
    deferred = client.post(f"/api/germplasm/duplicates/candidates/{candidate['id']}/decision", headers=headers, json={
        "action": "defer", "expected_version": candidate["version"], "actor": "审阅人", "reason": "待电话核实",
    })
    assert deferred.status_code == 200 and deferred.json()["status"] == "deferred"
    reopened = client.post(f"/api/germplasm/duplicates/candidates/{candidate['id']}/decision", headers=headers, json={
        "action": "reopen", "expected_version": deferred.json()["version"], "actor": "审阅人", "reason": "已核实",
    })
    assert reopened.status_code == 200 and reopened.json()["status"] == "open"
    current_version = reopened.json()["version"]

    # 未逐项决定冲突字段，预演被拒绝
    bad_preview = client.post(
        f"/api/germplasm/duplicates/candidates/{candidate['id']}/merge-preview", headers=headers, json={
            "kept_accession_id": pair["left"]["id"], "expected_version": current_version,
            "actor": "审阅人", "field_decisions": {},
        },
    )
    assert bad_preview.status_code == 409
    assert "source_id" in bad_preview.json()["error"]["context"]["conflicting_fields"]

    preview = client.post(
        f"/api/germplasm/duplicates/candidates/{candidate['id']}/merge-preview", headers=headers, json={
            "kept_accession_id": pair["left"]["id"],
            "expected_version": current_version,
            "actor": "审阅人",
            "reason": "确认为同一地方品种",
            "field_decisions": {"source_id": "kept", "acquisition_type": "kept"},
            "passport_key_decisions": {"origin_code": "kept"},
        },
    )
    assert preview.status_code == 201, preview.text
    merge = preview.json()
    assert merge["status"] == "previewed"
    by_field = {row["field"]: row for row in merge["plan"]["scalar_decisions"]}
    assert by_field["acquisition_type"]["kept_value"] == "采集"
    assert by_field["acquisition_type"]["retired_value"] == "交换"

    # 旧版本号执行被拒绝
    stale = client.post(f"/api/germplasm/merges/{merge['id']}/execute", headers=headers, json={
        "expected_version": current_version + 9,
        "idempotency_key": "api-merge-exec-1",
        "actor": "审阅人",
    })
    assert stale.status_code == 409

    executed = client.post(f"/api/germplasm/merges/{merge['id']}/execute", headers=headers, json={
        "expected_version": current_version,
        "idempotency_key": "api-merge-exec-1",
        "actor": "审阅人",
    })
    assert executed.status_code == 200, executed.text
    assert executed.json()["replayed"] is False

    # 幂等重放：同键再请求返回相同结果，不重复落档
    replayed = client.post(f"/api/germplasm/merges/{merge['id']}/execute", headers=headers, json={
        "expected_version": current_version,
        "idempotency_key": "api-merge-exec-1",
        "actor": "审阅人",
    })
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True

    # 旧资源号解析到保留档案
    resolved = client.get("/api/germplasm/accessions/by-no/API-STA-1", headers=headers)
    assert resolved.status_code == 200
    assert resolved.json()["id"] == pair["left"]["id"]
    assert resolved.json()["requested_accession"]["status"] == "merged"

    # 合并前后引用图均可重放
    before_graph = client.get(f"/api/germplasm/merges/{merge['id']}/graph/before", headers=headers)
    after_graph = client.get(f"/api/germplasm/merges/{merge['id']}/graph/after", headers=headers)
    assert before_graph.status_code == 200 and after_graph.status_code == 200
    assert after_graph.json()["graph"]["retired"]["merged_into"]["accession_id"] == pair["left"]["id"]

    # 候选终判为 merge_decided
    final_candidate = client.get(f"/api/germplasm/duplicates/candidates/{candidate['id']}", headers=headers)
    assert final_candidate.json()["status"] == "merge_decided"


def test_scan_requires_merge_permission(client, admin):
    # 未认证拒绝
    assert client.post("/api/germplasm/duplicates/scan", json={"actor": "x"}).status_code == 401
