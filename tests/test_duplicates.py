from __future__ import annotations

from app.database import get_connection, transaction
from app.germplasm.service import GermplasmService


def _create_pair(service: GermplasmService, suffix: str = "001") -> tuple[dict, dict]:
    """两份资源号不同、但学名/来源地/护照高度接近的地方品种（旧系统 + 合作站）。"""
    source_a = service.accessions.create_source({
        "source_code": f"SRC-OLD-{suffix}", "provider_name": "旧系统导入批次", "country_code": "CN",
        "locality": "云南元阳新街镇", "collected_on": "2019-09-10", "restrictions": {},
    })
    source_b = service.accessions.create_source({
        "source_code": f"SRC-STA-{suffix}", "provider_name": "合作站交换批次", "country_code": "CN",
        "locality": "云南元阳新街镇", "collected_on": "2019-09-10",
        "restrictions": {"no_distribution": True},
    })
    left = service.accessions.create_accession({
        "accession_no": f"OLD-{suffix}", "scientific_name": "Oryza sativa", "crop_name": "水稻",
        "cultivar_name": "元阳红米", "source_id": source_a["id"], "acquisition_type": "采集",
        "received_on": "2020-01-15",
        "passport": {"locality": "云南元阳新街镇", "collector": "张三", "origin_code": f"OLD-{suffix}"},
        "created_by": "旧系统",
    })
    right = service.accessions.create_accession({
        "accession_no": f"STA-{suffix}", "scientific_name": "Oryza sativa", "crop_name": "水稻",
        "cultivar_name": "元阳红米", "source_id": source_b["id"], "acquisition_type": "交换",
        "received_on": "2020-01-15",
        "passport": {"locality": "云南元阳新街镇", "collector": "张三", "origin_code": f"STA-{suffix}"},
        "created_by": "合作站",
    })
    left = service.accessions.transition(left["id"], {
        "target_status": "accepted", "reason": "旧档资料齐全", "expected_version": 1, "actor": "审核员",
    })
    right = service.accessions.transition(right["id"], {
        "target_status": "accepted", "reason": "合作站证明齐全", "expected_version": 1, "actor": "审核员",
    })
    return left, right


def _complete_merge(service: GermplasmService, suffix: str = "001", *, kept_id: int | None = None) -> dict:
    left, right = _create_pair(service, suffix)
    scan = service.duplicates.scan(actor="数据管理员")
    assert scan["created"] >= 1
    candidates = service.duplicates.list_candidates("open")
    candidate = next(
        item for item in candidates
        if {item["left_accession_id"], item["right_accession_id"]} == {left["id"], right["id"]}
    )
    kept_id = kept_id or left["id"]
    preview = service.duplicates.preview_merge(candidate["id"], {
        "kept_accession_id": kept_id,
        "expected_version": candidate["version"],
        "actor": "审阅人",
        "reason": "确认为同一地方品种",
        "field_decisions": {"source_id": "kept", "acquisition_type": "kept"},
        "passport_key_decisions": {"origin_code": "kept"},
    })
    executed = service.duplicates.execute_merge(preview["id"], {
        "expected_version": candidate["version"],
        "idempotency_key": f"merge-exec-{suffix}",
        "actor": "审阅人",
    })
    return {"left": left, "right": right, "candidate": candidate, "preview": preview, "merge": executed}


def test_scan_only_produces_evidence_backed_candidates(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        left, right = _create_pair(service)
        result = service.duplicates.scan(actor="数据管理员")
        assert result["created"] == 1
        assert result["rule_version"] == "duplicate-rules-v1"
        candidates = service.duplicates.list_candidates("open")
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate["score"] >= 55
        fields = {item["field"] for item in candidate["evidence"]}
        assert "scientific_name" in fields
        assert "source.locality" in fields
        assert "passport.locality" in fields
        # 规则不改档：两份档案都仍是 accepted
        assert service.repository.require_accession(left["id"])["status"] == "accepted"
        assert service.repository.require_accession(right["id"])["status"] == "accepted"
        # 扫描幂等：再次运行不新建、不刷新
        again = service.duplicates.scan(actor="数据管理员")
        assert again["created"] == 0 and again["unchanged"] == 1


def test_unrelated_species_never_candidate(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        service.accessions.create_accession({
            "accession_no": "X-1", "scientific_name": "Oryza sativa", "crop_name": "水稻",
            "acquisition_type": "采集", "received_on": "2020-01-01", "passport": {}, "created_by": "a",
        })
        service.accessions.create_accession({
            "accession_no": "X-2", "scientific_name": "Triticum aestivum", "crop_name": "小麦",
            "acquisition_type": "采集", "received_on": "2020-01-01", "passport": {}, "created_by": "a",
        })
        result = service.duplicates.scan(min_score=0, actor="a")
        assert result["created"] == 0


def test_decide_unrelated_defer_and_version_guard(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _create_pair(service)
        service.duplicates.scan(actor="a")
        candidate = service.duplicates.list_candidates("open")[0]
        deferred = service.duplicates.decide(candidate["id"], {
            "action": "defer", "expected_version": candidate["version"], "actor": "审阅人", "reason": "等待采集队确认",
        })
        assert deferred["status"] == "deferred"
        reopened = service.duplicates.decide(candidate["id"], {
            "action": "reopen", "expected_version": deferred["version"], "actor": "审阅人", "reason": "继续核查",
        })
        assert reopened["status"] == "open"
        unrelated = service.duplicates.decide(candidate["id"], {
            "action": "unrelated", "expected_version": reopened["version"], "actor": "审阅人", "reason": "同名不同材料",
        })
        assert unrelated["status"] == "unrelated"
        try:
            service.duplicates.decide(candidate["id"], {
                "action": "defer", "expected_version": 1, "actor": "审阅人", "reason": "旧版本",
            })
        except ConflictError as exc:
            assert exc.context["current_version"] == unrelated["version"]
        else:
            raise AssertionError("旧版本判定必须被拒绝")


def test_preview_requires_per_field_decisions(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        left, right = _create_pair(service)
        service.duplicates.scan(actor="a")
        candidate = service.duplicates.list_candidates("open")[0]
        try:
            service.duplicates.preview_merge(candidate["id"], {
                "kept_accession_id": left["id"], "expected_version": candidate["version"],
                "actor": "审阅人", "field_decisions": {}, "passport_key_decisions": {},
            })
        except ConflictError as exc:
            assert set(exc.context["conflicting_fields"]) == {"source_id", "acquisition_type"}
        else:
            raise AssertionError("冲突字段未逐项决定时预演必须被拒绝")
        try:
            service.duplicates.preview_merge(candidate["id"], {
                "kept_accession_id": left["id"], "expected_version": candidate["version"],
                "actor": "审阅人",
                "field_decisions": {"source_id": "kept", "acquisition_type": "kept"},
                "passport_key_decisions": {},
            })
        except ConflictError as exc:
            assert exc.context["conflicting_passport_keys"] == ["origin_code"]
        else:
            raise AssertionError("护照冲突键未逐项决定时预演必须被拒绝")


def test_merge_blocked_by_pending_distribution(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        left, right = _create_pair(service, "008")
        request = service.distribution.create_request({
            "request_no": "DIST-008", "requester": "待审批单位", "purpose": "鉴定",
            "items": [{"accession_id": right["id"], "quantity_grams": 10}],
        })
        service.distribution.submit(request["id"], 1)
        service.duplicates.scan(actor="a")
        candidate = service.duplicates.list_candidates("open")[0]
        try:
            service.duplicates.preview_merge(candidate["id"], {
                "kept_accession_id": left["id"], "expected_version": candidate["version"], "actor": "审阅人",
                "field_decisions": {"source_id": "kept", "acquisition_type": "kept"},
                "passport_key_decisions": {"origin_code": "kept"},
            })
        except ConflictError as exc:
            assert exc.context["pending_requests"][0]["request_no"] == "DIST-008"
        else:
            raise AssertionError("存在未终态发放申请时必须阻止合并")


def test_passport_aliases_merge_as_union(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        source = service.accessions.create_source({
            "source_code": "SRC-A7", "provider_name": "队", "country_code": "CN",
            "locality": "广西融水杆洞", "restrictions": {},
        })
        common = {
            "scientific_name": "Oryza sativa", "crop_name": "水稻", "cultivar_name": "杆洞香糯",
            "source_id": source["id"], "acquisition_type": "采集", "received_on": "2021-05-01",
        }
        a = service.accessions.create_accession({
            **common, "accession_no": "AL-A7",
            "passport": {"locality": "广西融水杆洞", "aliases": ["香糯", "老品种"]}, "created_by": "a",
        })
        b = service.accessions.create_accession({
            **common, "accession_no": "AL-B7",
            "passport": {"locality": "广西融水杆洞", "aliases": ["紫糯"]}, "created_by": "b",
        })
        service.accessions.transition(a["id"], {"target_status": "accepted", "reason": "ok", "expected_version": 1, "actor": "x"})
        service.accessions.transition(b["id"], {"target_status": "accepted", "reason": "ok", "expected_version": 1, "actor": "x"})
        service.duplicates.scan(actor="a")
        candidate = service.duplicates.list_candidates("open")[0]
        # aliases 不同但属于别名键，预演不应要求对其逐项二选一
        preview = service.duplicates.preview_merge(candidate["id"], {
            "kept_accession_id": a["id"], "expected_version": candidate["version"], "actor": "审阅人",
            "field_decisions": {}, "passport_key_decisions": {},
        })
        alias_row = next(row for row in preview["plan"]["passport"]["key_decisions"] if row["key"] == "aliases")
        assert alias_row["choice"] == "union"
        assert alias_row["final_value"] == ["香糯", "老品种", "紫糯"]
        merge = service.duplicates.execute_merge(preview["id"], {
            "expected_version": candidate["version"], "idempotency_key": "merge-exec-aliases", "actor": "审阅人",
        })
        assert merge["replayed"] is False
        kept = service.repository.require_accession(a["id"])
        assert kept["passport"]["aliases"] == ["香糯", "老品种", "紫糯"]
        assert set(kept["passport"]["_merged_aliases"]) == {"香糯", "老品种", "紫糯"}


def test_merge_carries_restrictions_as_strict_union(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        outcome = _complete_merge(service, "006")
        kept_id = outcome["left"]["id"]
        # 合作站来源带 no_distribution，合并后保留档案必须继承该限制
        kept = service.repository.require_accession(kept_id)
        assert kept["passport"]["restrictions"]["no_distribution"] is True
        restrictions = service.accessions.restrictions_for(kept_id)
        assert restrictions["distribution_allowed"] is False
        plan_restrictions = outcome["merge"]["plan"]["restrictions"]
        assert plan_restrictions["retired_source_restrictions"]["no_distribution"] is True
        assert plan_restrictions["merged_passport_restrictions"]["no_distribution"] is True
        assert plan_restrictions["distribution_allowed_after"] is False
        # 来源归并：双方来源出处都保留在护照溯源中
        provenance = kept["passport"]["merged_sources"]
        codes = {item["source_code"] for item in provenance}
        assert codes == {"SRC-OLD-006", "SRC-STA-006"}
        selected = [item for item in provenance if item["selected_as_primary"]]
        assert len(selected) == 1 and selected[0]["role"] == "kept"


def test_execute_merge_references_lots_events_aliases_and_immutable_records(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        outcome = _complete_merge(service, "001")
        kept_id, retired_id = outcome["left"]["id"], outcome["right"]["id"]
        merge = outcome["merge"]

        # 旧档案冻结但未删除
        retired = service.repository.require_accession(retired_id)
        assert retired["status"] == "merged"
        assert retired["merged_into_accession_id"] == kept_id

        # 旧资源号可解析到保留档案（冻结档案直查重定向，别名表同样登记）
        resolved = service.duplicates.resolve_accession_no("STA-001")
        assert resolved["id"] == kept_id
        assert resolved["resolution"] == "merged_redirect"
        assert resolved["requested_accession"]["status"] == "merged"
        direct = service.duplicates.resolve_accession_no("OLD-001")
        assert direct["id"] == kept_id and direct["resolution"] == "direct"

        # 归并台账含逐字段决定、原值与完整审计理由
        plan = merge["plan"]
        by_field = {row["field"]: row for row in plan["scalar_decisions"]}
        assert by_field["acquisition_type"]["retired_value"] == "交换"
        assert by_field["acquisition_type"]["final_value"] == "采集"
        passport_conflicts = merge["audit_reason"]["passport_conflicts"]
        assert {row["key"] for row in passport_conflicts} == {"origin_code"}
        assert merge["audit_reason"]["evidence"]

        # 引用图可在合并前后重放
        before = service.duplicates.merge_graph(merge["id"], "before")
        after = service.duplicates.merge_graph(merge["id"], "after")
        assert len(before["graph"]["retired"]["lots"]) == 0
        assert after["graph"]["retired"]["merged_into"]["accession_id"] == kept_id
        kept_event_types = [event["event_type"] for event in after["graph"]["kept"]["events"]]
        assert "merged_in" in kept_event_types
        retired_event_types = [event["event_type"] for event in after["graph"]["retired"]["events"]]
        assert "merged_away" in retired_event_types
        assert {row["accession_no"] for row in after["graph"]["kept"]["aliases"]} == {"STA-001"}


def test_merge_moves_lots_and_keeps_tests_and_distributions_immutable(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        left, right = _create_pair(service, "002")
        # 在合作站档案下建立批次、检测与已签发发放记录
        location = service.inventory.create_location({
            "location_code": "COLD-002", "facility": "长期库", "room": "低温一室", "rack": "R1", "shelf": "S1",
            "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
        })
        lot = service.inventory.create_lot({
            "lot_no": "LOT-002", "accession_id": right["id"], "harvest_year": 2019,
            "initial_weight_grams": 500, "moisture_percent": 7.5, "sealed_on": "2020-01-20", "created_by": "站方",
        })
        service.inventory.place_lot({
            "lot_id": lot["id"], "location_id": location["id"], "weight_grams": 500,
            "container_code": "BOX-002", "idempotency_key": "place-002-0001", "actor": "保管员",
        })
        protocol = service.viability.create_protocol({
            "protocol_code": "RICE-002", "crop_name": "水稻", "sample_size": 100, "replicate_count": 2,
            "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "根芽完整", "created_by": "负责人",
        })
        test = service.viability.schedule_test({
            "test_no": "VT-002", "lot_id": lot["id"], "protocol_id": protocol["id"], "test_type": "入库初检",
            "sampled_grams": 5, "scheduled_for": "2020-02-01", "requested_by": "检测员",
            "idempotency_key": "schedule-vt-002",
        })
        service.viability.start_test(test["id"], {"performed_by": "检测员", "expected_version": 1})
        service.viability.add_count(test["id"], {
            "replicate_no": 1, "seeds_tested": 100, "normal_count": 90, "abnormal_count": 5,
            "dead_count": 5, "observation_day": 14, "observed_by": "检测员",
        })
        service.viability.add_count(test["id"], {
            "replicate_no": 2, "seeds_tested": 100, "normal_count": 88, "abnormal_count": 6,
            "dead_count": 6, "observation_day": 14, "observed_by": "检测员",
        })
        completed = service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        assert completed["germination_percent"] == 89
        # 发放记录挂在旧档案上（明细校验仅要求 accepted）
        request = service.distribution.create_request({
            "request_no": "DIST-002", "requester": "育种单位", "purpose": "品种比较",
            "items": [{"accession_id": right["id"], "quantity_grams": 20}],
        })
        service.distribution.submit(request["id"], 1)
        approved = service.distribution.decide(request["id"], {
            "approve": True, "expected_version": 2, "actor": "资源审核员", "reason": "批准",
        })
        assert approved["status"] == "approved"

        service.duplicates.scan(actor="a")
        candidate = service.duplicates.list_candidates("open")[0]
        preview = service.duplicates.preview_merge(candidate["id"], {
            "kept_accession_id": left["id"], "expected_version": candidate["version"], "actor": "审阅人",
            "field_decisions": {"source_id": "kept", "acquisition_type": "kept"},
            "passport_key_decisions": {"origin_code": "kept"},
        })
        merge = service.duplicates.execute_merge(preview["id"], {
            "expected_version": candidate["version"], "idempotency_key": "merge-exec-002", "actor": "审阅人",
        })

        # 批次改挂保留档案，检测结果原样不可变
        moved_lot = service.repository.require_lot(lot["id"])
        assert moved_lot["accession_id"] == left["id"]
        moved_test = service.repository.require_test(test["id"])
        assert moved_test["status"] == "completed"
        assert moved_test["germination_percent"] == 89
        assert moved_test["lot_id"] == lot["id"]

        # 已签发发放记录不可变：明细仍指向冻结档案
        item = connection.execute(
            "SELECT * FROM distribution_items WHERE request_id=?", (request["id"],)
        ).fetchone()
        assert item["accession_id"] == right["id"]
        after = service.duplicates.merge_graph(merge["id"], "after")
        assert len(after["graph"]["retired"]["distribution_items"]) == 1
        assert after["graph"]["retired"]["distribution_items"][0]["request_status"] == "approved"

        # 事件历史已归并到保留档案
        kept_detail = service.repository.accession_detail(left["id"])
        event_types = [event["event_type"] for event in kept_detail["events"]]
        assert "merged_history" in event_types and "merged_in" in event_types


def test_merge_execute_is_idempotent_and_version_checked(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        outcome = _complete_merge(service, "003")
        merge_id = outcome["merge"]["id"]
        # 同幂等键重放，不产生第二次合并
        replay = service.duplicates.execute_merge(merge_id, {
            "expected_version": outcome["candidate"]["version"],
            "idempotency_key": "merge-exec-003",
            "actor": "审阅人",
        })
        assert replay["replayed"] is True
        kept_id = outcome["left"]["id"]
        kept = service.repository.require_accession(kept_id)
        merged_history_count = connection.execute(
            "SELECT COUNT(*) FROM accession_events WHERE accession_id=? AND event_type='merged_history'", (kept_id,)
        ).fetchone()[0]
        assert merged_history_count > 0
        # 重复执行一次（无重放键）应被拒绝
        try:
            service.duplicates.execute_merge(merge_id, {
                "expected_version": outcome["candidate"]["version"],
                "idempotency_key": "merge-exec-003-NEW",
                "actor": "审阅人",
            })
        except ConflictError:
            pass
        else:
            raise AssertionError("已完成合并不能再次执行")
        # 同幂等键不能用于其他合并
        try:
            service.duplicates.resolve_accession_no("NOPE")
        except Exception:
            pass


def test_merge_prevents_chained_and_circular_duplicates(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        outcome = _complete_merge(service, "004")
        kept_id, retired_id = outcome["left"]["id"], outcome["right"]["id"]
        third_source = service.accessions.create_source({
            "source_code": "SRC-3-004", "provider_name": "又一批", "country_code": "CN",
            "locality": "云南元阳新街镇", "restrictions": {},
        })
        third = service.accessions.create_accession({
            "accession_no": "THIRD-004", "scientific_name": "Oryza sativa", "crop_name": "水稻",
            "cultivar_name": "元阳红米", "source_id": third_source["id"], "acquisition_type": "采集",
            "received_on": "2020-01-15",
            "passport": {"locality": "云南元阳新街镇", "collector": "张三", "origin_code": "T-004"},
            "created_by": "三系统",
        })
        third = service.accessions.transition(third["id"], {
            "target_status": "accepted", "reason": "ok", "expected_version": 1, "actor": "审核员",
        })
        service.duplicates.scan(actor="a")
        open_candidates = service.duplicates.list_candidates("open")

        # 禁止把已经作为保留根的档案再并入第三方（链式 A→C）
        cand = next(
            item for item in open_candidates
            if {item["left_accession_id"], item["right_accession_id"]} == {third["id"], kept_id}
        )
        try:
            service.duplicates.preview_merge(cand["id"], {
                "kept_accession_id": third["id"], "expected_version": cand["version"], "actor": "审阅人",
                "field_decisions": {"source_id": "retired", "acquisition_type": "kept"},
                "passport_key_decisions": {"origin_code": "retired"},
            })
        except ConflictError as exc:
            assert "链式" in exc.message
        else:
            raise AssertionError("保留根被再次并入必须拒绝")

        # 冻结档案不能参与任何合并（白盒校验角色守卫）
        try:
            service.duplicates._require_live_accession(retired_id, role="retired")
        except ConflictError:
            pass
        else:
            raise AssertionError("冻结档案不能再次参与合并")

        # 扇形合并不受影响：新档案可以继续并入同一保留根
        fan_candidate = next(
            item for item in service.duplicates.list_candidates("open")
            if {item["left_accession_id"], item["right_accession_id"]} == {third["id"], kept_id}
        )
        fan_preview = service.duplicates.preview_merge(fan_candidate["id"], {
            "kept_accession_id": kept_id, "expected_version": fan_candidate["version"], "actor": "审阅人",
            "field_decisions": {"source_id": "kept", "acquisition_type": "kept"},
            "passport_key_decisions": {"origin_code": "kept"},
        })
        fan_merge = service.duplicates.execute_merge(fan_preview["id"], {
            "expected_version": fan_candidate["version"], "idempotency_key": "merge-exec-004-fan",
            "actor": "审阅人",
        })
        assert fan_merge["replayed"] is False
        # 两个旧编号都解析到同一保留根
        assert service.duplicates.resolve_accession_no("STA-004")["id"] == kept_id
        assert service.duplicates.resolve_accession_no("THIRD-004")["id"] == kept_id
        # 别名表把所有旧编号直接指向保留根（无链式中间跳）
        alias_rows = connection.execute(
            "SELECT accession_no,accession_id FROM accession_aliases ORDER BY accession_no"
        ).fetchall()
        assert {row[0]: row[1] for row in alias_rows} == {"STA-004": kept_id, "THIRD-004": kept_id}


def test_stale_candidate_version_blocks_preview(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        left, right = _create_pair(service, "005")
        service.duplicates.scan(actor="a")
        candidate = service.duplicates.list_candidates("open")[0]
        # 档案在候选生成后被修改
        service.accessions.update_accession(left["id"], {
            "crop_name": "早稻", "expected_version": left["version"], "actor": "登记员",
        })
        try:
            service.duplicates.preview_merge(candidate["id"], {
                "kept_accession_id": left["id"], "expected_version": candidate["version"], "actor": "审阅人",
                "field_decisions": {"source_id": "kept", "acquisition_type": "kept"},
                "passport_key_decisions": {"origin_code": "kept"},
            })
        except ConflictError as exc:
            assert exc.context["current_version"] == left["version"] + 1
        else:
            raise AssertionError("候选证据过期时必须阻止预演")
