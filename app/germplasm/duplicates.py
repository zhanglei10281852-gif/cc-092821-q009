from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.germplasm.repository import GermplasmRepository, record, records

RULE_VERSION = "duplicate-rules-v1"
DEFAULT_MIN_SCORE = 55.0
MAX_MERGE_DEPTH = 100

SCALAR_FIELDS = ("scientific_name", "crop_name", "cultivar_name", "source_id", "acquisition_type", "received_on")

PASSPORT_LOCALITY_KEYS = {"locality", "site", "location", "采集地点", "采集地", "地点", "原产地"}
PASSPORT_COLLECTOR_KEYS = {"collector", "collector_name", "collectorname", "采集者", "采集人"}
PASSPORT_LAT_KEYS = {"latitude", "lat", "纬度"}
PASSPORT_LNG_KEYS = {"longitude", "lng", "long", "经度"}
PASSPORT_ALT_KEYS = {"altitude", "elevation", "海拔"}
PASSPORT_ALIAS_KEYS = {"aliases", "alias", "别名", "地方名", "俗名", "other_names"}

_JSON_COLUMNS = {
    "evidence_json": "evidence",
    "snapshot_json": "snapshot",
    "field_decisions_json": "plan",
    "restrictions_merged_json": "restrictions_merged",
    "before_graph_json": "before_graph",
    "after_graph_json": "after_graph",
    "audit_reason_json": "audit_reason",
}


def mrecord(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for column, target in _JSON_COLUMNS.items():
        if column in data:
            raw = data.pop(column)
            try:
                data[target] = json.loads(raw or "{}")
            except json.JSONDecodeError:
                data[target] = {}
    return data


def mrecords(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [mrecord(row) or {} for row in rows]


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip().casefold())


def _norm_no_punct(value: Any) -> str:
    return re.sub(r"[^0-9a-z一-鿿 ]+", "", _norm_text(value))


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _passport_value(passport: dict[str, Any], names: set[str]) -> tuple[str | None, Any]:
    wanted = {_norm_text(name) for name in names}
    for key, value in passport.items():
        if _norm_text(key) in wanted:
            return key, value
    return None, None


def _values_differ(field: str, left: Any, right: Any) -> bool:
    if left is None and right is None:
        return False
    if left is None or right is None:
        return True
    if field in {"scientific_name", "crop_name", "cultivar_name", "acquisition_type"}:
        return _norm_text(left) != _norm_text(right)
    return str(left) != str(right)


def _union_restrictions(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in set(left) | set(right):
        lv, rv = left.get(key), right.get(key)
        if isinstance(lv, bool) or isinstance(rv, bool):
            merged[key] = bool(lv) or bool(rv)
        elif isinstance(lv, list) or isinstance(rv, list):
            values: list[Any] = []
            for value in [*(lv if isinstance(lv, list) else []), *(rv if isinstance(rv, list) else [])]:
                if value not in values:
                    values.append(value)
            merged[key] = values
        else:
            merged[key] = lv if lv is not None else rv
    return merged


class DuplicateRules:
    """疑似重复评分规则：只产出字段证据与评分，绝不修改任何档案。"""

    version = RULE_VERSION

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def compare(self, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
        evidence: list[dict[str, Any]] = []
        score = 0.0

        sci_left = _norm_no_punct(left["scientific_name"])
        sci_right = _norm_no_punct(right["scientific_name"])
        if not sci_left or sci_left != sci_right:
            return None
        score += _add_evidence(evidence, "scientific_name", left["scientific_name"], right["scientific_name"], 30, "exact")

        if _norm_text(left["crop_name"]) and _norm_text(left["crop_name"]) == _norm_text(right["crop_name"]):
            score += _add_evidence(evidence, "crop_name", left["crop_name"], right["crop_name"], 5, "exact")

        cultivar = _cultivar_match(left.get("cultivar_name", ""), right.get("cultivar_name", ""))
        if cultivar:
            relation, points = cultivar
            score += _add_evidence(evidence, "cultivar_name", left.get("cultivar_name", ""), right.get("cultivar_name", ""), points, relation)

        left_source = self._source(left)
        right_source = self._source(right)
        if left_source and right_source:
            if _norm_text(left_source.get("country_code")) == _norm_text(right_source.get("country_code")):
                score += _add_evidence(
                    evidence, "source.country_code", left_source.get("country_code"), right_source.get("country_code"), 5, "exact"
                )
            if _norm_text(left_source.get("locality")) and _norm_text(left_source.get("locality")) == _norm_text(right_source.get("locality")):
                score += _add_evidence(
                    evidence, "source.locality", left_source.get("locality"), right_source.get("locality"), 15, "exact"
                )
            if left_source.get("collected_on") and left_source.get("collected_on") == right_source.get("collected_on"):
                score += _add_evidence(
                    evidence, "source.collected_on", left_source.get("collected_on"), right_source.get("collected_on"), 5, "exact"
                )

        if left.get("received_on") and left.get("received_on") == right.get("received_on"):
            score += _add_evidence(evidence, "received_on", left["received_on"], right["received_on"], 5, "exact")

        score += self._passport_evidence(evidence, left.get("passport", {}), right.get("passport", {}))
        return {"score": round(min(score, 100.0), 2), "evidence": evidence}

    def _source(self, accession: dict[str, Any]) -> dict[str, Any] | None:
        source_id = accession.get("source_id")
        if not source_id:
            return None
        return record(self.connection.execute("SELECT * FROM collection_sources WHERE id=?", (source_id,)).fetchone())

    def _passport_evidence(self, evidence: list[dict[str, Any]], left: dict[str, Any], right: dict[str, Any]) -> float:
        score = 0.0
        lkey, lvalue = _passport_value(left, PASSPORT_LOCALITY_KEYS)
        rkey, rvalue = _passport_value(right, PASSPORT_LOCALITY_KEYS)
        if lvalue is not None and rvalue is not None and _norm_text(lvalue) == _norm_text(rvalue):
            score += _add_evidence(evidence, f"passport.{lkey}", lvalue, rvalue, 15, "exact")
        lkey, lvalue = _passport_value(left, PASSPORT_COLLECTOR_KEYS)
        rkey, rvalue = _passport_value(right, PASSPORT_COLLECTOR_KEYS)
        if lvalue and rvalue and _norm_text(lvalue) == _norm_text(rvalue):
            score += _add_evidence(evidence, f"passport.{lkey}", lvalue, rvalue, 8, "exact")
        for names, label, close_at, near_at, close_points, near_points in (
            (PASSPORT_LAT_KEYS, "latitude", 0.01, 0.1, 8, 4),
            (PASSPORT_LNG_KEYS, "longitude", 0.01, 0.1, 8, 4),
            (PASSPORT_ALT_KEYS, "altitude", 20, 100, 4, 2),
        ):
            _, lnum = _passport_value(left, names)
            _, rnum = _passport_value(right, names)
            lf, rf = _to_float(lnum), _to_float(rnum)
            if lf is not None and rf is not None:
                delta = abs(lf - rf)
                if delta <= close_at:
                    score += _add_evidence(evidence, f"passport.{label}", lnum, rnum, close_points, "close")
                elif delta <= near_at:
                    score += _add_evidence(evidence, f"passport.{label}", lnum, rnum, near_points, "near")
        _, laliases = _passport_value(left, PASSPORT_ALIAS_KEYS)
        _, raliases = _passport_value(right, PASSPORT_ALIAS_KEYS)
        la, ra = _alias_set(laliases), _alias_set(raliases)
        if la and ra and la & ra:
            score += _add_evidence(evidence, "passport.aliases", sorted(la), sorted(ra), 8, "overlap")
        return score


def _alias_set(value: Any) -> set[str]:
    return {_norm_text(part) for part in _flatten_alias_values(value) if str(part).strip()}


def _flatten_alias_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [part for part in value if str(part).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[;,，、/]+", value) if part.strip()]
    return [value]


def _union_alias_values(left: Any, right: Any) -> list[Any]:
    merged: list[Any] = []
    for value in _flatten_alias_values(left) + _flatten_alias_values(right):
        if value not in merged:
            merged.append(value)
    return merged


def _cultivar_match(left: str, right: str) -> tuple[str, float] | None:
    a, b = _norm_text(left), _norm_text(right)
    if not a or not b:
        return None
    if a == b:
        return "exact", 10
    tokens_a = {token for token in re.split(r"[\s,，、/]+", a) if token}
    tokens_b = {token for token in re.split(r"[\s,，、/]+", b) if token}
    if tokens_a and tokens_b and tokens_a & tokens_b:
        return "token_overlap", 5
    if a in b or b in a:
        return "contained", 5
    return None


def _add_evidence(evidence: list[dict[str, Any]], field: str, left: Any, right: Any, points: float, relation: str) -> float:
    evidence.append({"field": field, "left": left, "right": right, "relation": relation, "points": points})
    return points


class DuplicateService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)
        self.rules = DuplicateRules(connection)

    # ------------------------------------------------------------------ 扫描

    def scan(self, *, min_score: float = DEFAULT_MIN_SCORE, actor: str = "system") -> dict[str, Any]:
        if not 0 <= min_score <= 100:
            raise ValidationError("评分阈值必须在 0 到 100 之间")
        rows = self.connection.execute(
            "SELECT * FROM accessions WHERE status <> 'merged' ORDER BY id"
        ).fetchall()
        accessions = records(rows)
        groups: dict[str, list[dict[str, Any]]] = {}
        for accession in accessions:
            key = _norm_no_punct(accession["scientific_name"])
            if key:
                groups.setdefault(key, []).append(accession)
        created = refreshed = unchanged = below_threshold = 0
        candidate_ids: list[int] = []
        for group in groups.values():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    left, right = group[i], group[j]
                    result = self.rules.compare(left, right)
                    if result is None or result["score"] < min_score:
                        below_threshold += 1
                        continue
                    outcome, candidate_id = self._upsert_candidate(left, right, result)
                    created += int(outcome == "created")
                    refreshed += int(outcome == "refreshed")
                    unchanged += int(outcome == "unchanged")
                    candidate_ids.append(candidate_id)
        timestamp = to_storage(self.clock.now())
        summary = {
            "rule_version": RULE_VERSION, "min_score": min_score,
            "created": created, "refreshed": refreshed, "unchanged": unchanged,
            "below_threshold": below_threshold, "candidate_ids": candidate_ids,
        }
        self._append_audit("duplicate.scan", actor, "duplicate_candidate", None, summary, timestamp)
        return {"scanned_accessions": len(accessions), **summary}

    def _upsert_candidate(self, left: dict[str, Any], right: dict[str, Any], result: dict[str, Any]) -> tuple[str, int]:
        lo_id, hi_id = sorted((int(left["id"]), int(right["id"])))
        candidate_key = f"{RULE_VERSION}:{lo_id}:{hi_id}"
        timestamp = to_storage(self.clock.now())
        snapshot = self._candidate_snapshot(lo_id, hi_id)
        evidence_json = json.dumps(result["evidence"], ensure_ascii=False, sort_keys=True)
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        existing = mrecord(self.connection.execute(
            "SELECT * FROM duplicate_candidates WHERE candidate_key=?", (candidate_key,)
        ).fetchone())
        if existing is None:
            cursor = self.connection.execute(
                "INSERT INTO duplicate_candidates(candidate_key,left_accession_id,right_accession_id,rule_version,score,"
                "evidence_json,status,snapshot_json,created_at,updated_at) VALUES(?,?,?,?,?,?,'open',?,?,?)",
                (candidate_key, lo_id, hi_id, RULE_VERSION, result["score"], evidence_json, snapshot_json, timestamp, timestamp),
            )
            return "created", int(cursor.lastrowid)
        if existing["status"] in {"unrelated", "merge_decided"}:
            return "unchanged", int(existing["id"])
        if (
            float(existing["score"]) == float(result["score"])
            and existing["evidence"] == result["evidence"]
            and existing["snapshot"] == snapshot
        ):
            return "unchanged", int(existing["id"])
        self.connection.execute(
            "UPDATE duplicate_candidates SET score=?,evidence_json=?,snapshot_json=?,version=version+1,updated_at=? WHERE id=?",
            (result["score"], evidence_json, snapshot_json, timestamp, existing["id"]),
        )
        return "refreshed", int(existing["id"])

    def _candidate_snapshot(self, left_id: int, right_id: int) -> dict[str, Any]:
        return {"left": self._accession_snapshot(left_id), "right": self._accession_snapshot(right_id)}

    def _accession_snapshot(self, accession_id: int) -> dict[str, Any]:
        accession = self.repository.require_accession(accession_id)
        return {key: accession[key] for key in (
            "id", "accession_no", "version", "scientific_name", "crop_name", "cultivar_name",
            "source_id", "acquisition_type", "received_on", "status",
        )} | {"passport": accession.get("passport", {})}

    # ------------------------------------------------------------------ 查询

    def list_candidates(self, status: str | None = None) -> list[dict[str, Any]]:
        valid = {"open", "deferred", "unrelated", "merge_decided", "superseded", "invalid"}
        if status:
            if status not in valid:
                raise ValidationError("候选状态无效")
            rows = self.connection.execute(
                "SELECT * FROM duplicate_candidates WHERE status=? ORDER BY score DESC,id", (status,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM duplicate_candidates ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'deferred' THEN 1 ELSE 2 END,score DESC,id"
            ).fetchall()
        return mrecords(rows)

    def candidate_detail(self, candidate_id: int) -> dict[str, Any]:
        candidate = self._require_candidate(candidate_id)
        candidate["left_accession"] = self.repository.accession_detail(int(candidate["left_accession_id"]))
        candidate["right_accession"] = self.repository.accession_detail(int(candidate["right_accession_id"]))
        if candidate.get("merge_id"):
            candidate["merge"] = self.merge_detail(int(candidate["merge_id"]), include_graphs=False)
        return candidate

    def _require_candidate(self, candidate_id: int) -> dict[str, Any]:
        candidate = mrecord(self.connection.execute(
            "SELECT * FROM duplicate_candidates WHERE id=?", (candidate_id,)
        ).fetchone())
        if candidate is None:
            raise NotFoundError("疑似重复候选不存在")
        return candidate

    # ------------------------------------------------------------------ 人工判定

    def decide(self, candidate_id: int, data: dict[str, Any]) -> dict[str, Any]:
        candidate = self._require_candidate(candidate_id)
        self._check_candidate_version(candidate, int(data["expected_version"]))
        action = data["action"]
        if action not in {"unrelated", "defer", "reopen"}:
            raise ValidationError("判定动作必须是 unrelated、defer 或 reopen")
        if candidate["status"] == "merge_decided":
            raise ConflictError("候选已经确认合并，不能再改判")
        transitions = {
            "unrelated": {"open", "deferred", "superseded", "invalid"},
            "defer": {"open", "deferred"},
            "reopen": {"deferred", "unrelated", "superseded", "invalid"},
        }
        if candidate["status"] not in transitions[action]:
            raise ConflictError("当前候选状态不允许该判定", context={"status": candidate["status"]})
        if action in {"unrelated", "defer"} and not str(data.get("reason", "")).strip():
            raise ValidationError("判定无关或暂缓时必须填写原因")
        new_status = {"unrelated": "unrelated", "defer": "deferred", "reopen": "open"}[action]
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE duplicate_candidates SET status=?,decision_reason=?,decided_by=?,decided_at=?,"
            "version=version+1,updated_at=? WHERE id=?",
            (
                new_status, str(data.get("reason", "")).strip(), data["actor"], timestamp, timestamp, candidate_id,
            ),
        )
        self._append_audit(f"duplicate.{action}", data["actor"], "duplicate_candidate", candidate_id, {
            "reason": str(data.get("reason", "")).strip(),
            "left_accession_id": candidate["left_accession_id"],
            "right_accession_id": candidate["right_accession_id"],
        }, timestamp)
        return self._require_candidate(candidate_id)

    def _check_candidate_version(self, candidate: dict[str, Any], expected_version: int) -> None:
        if int(candidate["version"]) != expected_version:
            raise ConflictError("候选记录已被更新，请重新查看证据后再判定", context={
                "current_version": candidate["version"]
            })

    # ------------------------------------------------------------------ 预演

    def preview_merge(self, candidate_id: int, data: dict[str, Any]) -> dict[str, Any]:
        candidate = self._require_candidate(candidate_id)
        self._check_candidate_version(candidate, int(data["expected_version"]))
        if candidate["status"] in {"merge_decided", "unrelated"}:
            raise ConflictError("候选状态不允许发起合并", context={"status": candidate["status"]})
        kept_id, retired_id = self._resolve_pair(candidate, int(data["kept_accession_id"]))
        kept = self._require_live_accession(kept_id, role="kept")
        retired = self._require_live_accession(retired_id, role="retired")
        self._assert_snapshots_current(candidate, [kept, retired])
        self._assert_no_pending_distributions(retired_id)

        plan = self._build_merge_plan(kept, retired, data.get("field_decisions", {}), data.get("passport_key_decisions", {}))
        plan["closure"] = self._preview_closure(kept_id, retired_id)
        fingerprint = self._plan_fingerprint(plan)
        timestamp = to_storage(self.clock.now())
        before_graph = {"kept": self.build_graph(kept_id), "retired": self.build_graph(retired_id)}
        audit_reason = self._build_audit_reason(candidate, kept, retired, plan, fingerprint, data["actor"], timestamp,
                                                reason=str(data.get("reason", "")).strip())

        existing = mrecord(self.connection.execute(
            "SELECT * FROM accession_merges WHERE candidate_id=? AND status='previewed'", (candidate_id,)
        ).fetchone())
        if existing:
            if existing["audit_reason"].get("plan_fingerprint") == fingerprint \
                    and int(existing["kept_accession_id"]) == kept_id \
                    and int(existing["retired_accession_id"]) == retired_id:
                # 相同预演请求：幂等返回，不写新事件、不升版本。
                return self.merge_detail(int(existing["id"]), include_graphs=True)
            self.connection.execute(
                "UPDATE accession_merges SET kept_accession_id=?,retired_accession_id=?,candidate_version=?,"
                "field_decisions_json=?,restrictions_merged_json=?,before_graph_json=?,audit_reason_json=?,updated_at=? WHERE id=?",
                (
                    kept_id, retired_id, int(candidate["version"]),
                    json.dumps(plan, ensure_ascii=False, sort_keys=True),
                    json.dumps(plan["restrictions"], ensure_ascii=False, sort_keys=True),
                    json.dumps(before_graph, ensure_ascii=False, sort_keys=True),
                    json.dumps(audit_reason, ensure_ascii=False, sort_keys=True), timestamp, existing["id"],
                ),
            )
            merge_id = int(existing["id"])
            self._append_merge_event(merge_id, "preview_updated", {"by": data["actor"]}, timestamp)
        else:
            cursor = self.connection.execute(
                "INSERT INTO accession_merges(merge_no,kept_accession_id,retired_accession_id,candidate_id,candidate_version,"
                "field_decisions_json,restrictions_merged_json,before_graph_json,audit_reason_json,created_by,created_at,updated_at) "
                "VALUES('',?,?,?,?,?,?,?,?,?,?,?)",
                (
                    kept_id, retired_id, candidate_id, int(candidate["version"]),
                    json.dumps(plan, ensure_ascii=False, sort_keys=True),
                    json.dumps(plan["restrictions"], ensure_ascii=False, sort_keys=True),
                    json.dumps(before_graph, ensure_ascii=False, sort_keys=True),
                    json.dumps(audit_reason, ensure_ascii=False, sort_keys=True),
                    data["actor"], timestamp, timestamp,
                ),
            )
            merge_id = int(cursor.lastrowid)
            self.connection.execute("UPDATE accession_merges SET merge_no=? WHERE id=?", (f"MRG-{merge_id:06d}", merge_id))
            self._append_merge_event(merge_id, "previewed", {"by": data["actor"]}, timestamp)
        self._append_audit("duplicate.merge.preview", data["actor"], "accession_merge", merge_id, {
            "candidate_id": candidate_id, "kept_accession_id": kept_id, "retired_accession_id": retired_id,
            "plan_fingerprint": fingerprint, "reason": audit_reason["reason"],
        }, timestamp)
        return self.merge_detail(merge_id, include_graphs=True)

    def _resolve_pair(self, candidate: dict[str, Any], kept_accession_id: int) -> tuple[int, int]:
        left_id = int(candidate["left_accession_id"])
        right_id = int(candidate["right_accession_id"])
        if kept_accession_id not in {left_id, right_id}:
            raise ValidationError("保留档案必须是候选中的两份档案之一")
        return kept_accession_id, right_id if kept_accession_id == left_id else left_id

    def _require_live_accession(self, accession_id: int, *, role: str) -> dict[str, Any]:
        accession = self.repository.require_accession(accession_id)
        if accession["status"] == "merged" or accession.get("merged_into_accession_id") is not None:
            raise ConflictError("档案已在合并链中冻结，不能再次参与合并", context={"accession_id": accession_id})
        if role == "retired" and self._was_kept_before(accession_id):
            raise ConflictError("该档案已作为保留档案完成过合并，不能再被并入其他档案（防止链式重复合并）", context={
                "accession_id": accession_id
            })
        if self._resolve_root(accession_id) != accession_id:
            raise ConflictError("档案已经被别名指向其他保留档案，禁止链式合并", context={"accession_id": accession_id})
        return accession

    def _was_kept_before(self, accession_id: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM accession_merges WHERE kept_accession_id=? AND status='completed' LIMIT 1",
            (accession_id,),
        ).fetchone() is not None

    def _assert_no_pending_distributions(self, retired_id: int) -> None:
        rows = self.connection.execute(
            "SELECT r.request_no,r.status FROM distribution_items i "
            "JOIN distribution_requests r ON r.id=i.request_id "
            "WHERE i.accession_id=? AND r.status IN ('draft','submitted') ORDER BY r.request_no",
            (retired_id,),
        ).fetchall()
        if rows:
            raise ConflictError("待合并档案存在未终态发放申请，请先处理或取消后再合并", context={
                "pending_requests": [{"request_no": row[0], "status": row[1]} for row in rows]
            })

    def _assert_snapshots_current(self, candidate: dict[str, Any], accessions: list[dict[str, Any]]) -> None:
        snapshot = candidate["snapshot"]
        by_id = {int(item["id"]): item for item in (snapshot["left"], snapshot["right"])}
        for accession in accessions:
            snap = by_id.get(int(accession["id"]))
            if snap is None or int(snap["version"]) != int(accession["version"]):
                raise ConflictError("候选证据生成后档案发生变化，请重新运行扫描并复核", context={
                    "accession_id": accession["id"],
                    "snapshot_version": snap["version"] if snap else None,
                    "current_version": accession["version"],
                })

    def _build_merge_plan(
        self,
        kept: dict[str, Any],
        retired: dict[str, Any],
        field_decisions: dict[str, Any],
        passport_key_decisions: dict[str, str],
    ) -> dict[str, Any]:
        scalar_rows: list[dict[str, Any]] = []
        final_scalars: dict[str, Any] = {}
        undecided: list[str] = []
        for field in SCALAR_FIELDS:
            kept_value = kept.get(field)
            retired_value = retired.get(field)
            different = _values_differ(field, kept_value, retired_value)
            directive = field_decisions.get(field)
            if isinstance(directive, dict):
                directive = directive.get("choice")
            choice = "kept"
            if different:
                if directive is None:
                    undecided.append(field)
                else:
                    if directive not in {"kept", "retired"}:
                        raise ValidationError(f"字段 {field} 的保留决定无效")
                    choice = directive
            elif directive not in (None, "kept"):
                raise ValidationError(f"字段 {field} 没有冲突，无需选择")
            final_scalars[field] = kept_value if choice == "kept" else retired_value
            scalar_rows.append({
                "field": field,
                "conflict": different,
                "kept_value": kept_value,
                "retired_value": retired_value,
                "choice": choice,
                "final_value": final_scalars[field],
            })
        if undecided:
            raise ConflictError("存在未逐项决定的冲突字段", context={"conflicting_fields": undecided})
        unknown = set(field_decisions) - set(SCALAR_FIELDS) - {"passport"}
        if unknown:
            raise ValidationError(f"存在不支持的合并字段: {sorted(unknown)}")

        passport_plan = self._merge_passport(
            kept.get("passport", {}), retired.get("passport", {}),
            field_decisions.get("passport"), passport_key_decisions,
        )
        restrictions = self._merge_restrictions(kept, retired)
        passport_plan = self._attach_source_provenance(passport_plan, kept, retired, final_scalars.get("source_id"))
        return {
            "scalar_decisions": scalar_rows,
            "final_scalars": final_scalars,
            "passport": passport_plan,
            "restrictions": restrictions,
        }

    def _attach_source_provenance(
        self, passport_plan: dict[str, Any], kept: dict[str, Any], retired: dict[str, Any], chosen_source_id: Any
    ) -> dict[str, Any]:
        provenance: list[dict[str, Any]] = []
        for accession, role in ((kept, "kept"), (retired, "retired")):
            source = self._source_for(accession)
            if source is None:
                continue
            provenance.append({
                "source_id": source["id"],
                "source_code": source["source_code"],
                "provider_name": source["provider_name"],
                "country_code": source["country_code"],
                "locality": source.get("locality", ""),
                "collected_on": source.get("collected_on"),
                "from_accession_no": accession["accession_no"],
                "role": role,
                "selected_as_primary": source["id"] == chosen_source_id,
            })
        merged = dict(passport_plan["merged"])
        previous = [item for item in merged.get("merged_sources", []) if isinstance(item, dict)]
        for item in provenance:
            if not any(existing.get("source_id") == item["source_id"] for existing in previous):
                previous.append(item)
        if previous:
            merged["merged_sources"] = previous
        passport_plan["merged"] = merged
        passport_plan["source_provenance"] = provenance
        return passport_plan

    def _merge_passport(
        self,
        kept_passport: dict[str, Any],
        retired_passport: dict[str, Any],
        directive: Any,
        key_decisions: dict[str, str],
    ) -> dict[str, Any]:
        if isinstance(directive, dict) and isinstance(directive.get("keys"), dict):
            key_decisions = {**key_decisions, **directive["keys"]}
        if isinstance(directive, dict) and directive.get("choice") == "kept":
            return {"strategy": "kept", "merged": dict(kept_passport), "key_decisions": []}
        if directive is not None and not isinstance(directive, dict):
            raise ValidationError("护照合并策略格式无效")
        if isinstance(directive, dict) and directive.get("choice") not in (None, "union", "kept"):
            raise ValidationError("护照合并策略只能是 union 或 kept")

        merged: dict[str, Any] = dict(kept_passport)
        key_rows: list[dict[str, Any]] = []
        normalized_decisions = {_norm_text(key): value for key, value in key_decisions.items()}
        alias_keys = {_norm_text(name) for name in PASSPORT_ALIAS_KEYS}
        undecided: list[str] = []
        for key in sorted(set(kept_passport) | set(retired_passport), key=_norm_text):
            in_kept = key in kept_passport
            in_retired = key in retired_passport
            kept_value = kept_passport.get(key)
            retired_value = retired_passport.get(key)
            different = (
                in_kept and in_retired
                and json.dumps(kept_value, ensure_ascii=False, sort_keys=True)
                != json.dumps(retired_value, ensure_ascii=False, sort_keys=True)
            )
            origin = "both" if in_kept and in_retired else ("kept" if in_kept else "retired")
            choice = "kept"
            if different and _norm_text(key) in alias_keys:
                # 护照别名类字段安全归并：自动去重并集，不做二选一。
                union_value = _union_alias_values(kept_value, retired_value)
                merged[key] = union_value
                choice = "union"
                key_rows.append({
                    "key": key,
                    "origin": origin,
                    "conflict": False,
                    "merged_alias": True,
                    "kept_value": kept_value if in_kept else None,
                    "retired_value": retired_value if in_retired else None,
                    "choice": "union",
                    "final_value": union_value,
                })
                continue
            if different:
                choice = normalized_decisions.get(_norm_text(key))
                if choice is None:
                    undecided.append(key)
                    choice = "kept"
                elif choice not in {"kept", "retired"}:
                    raise ValidationError(f"护照字段 {key} 的保留决定无效")
            if origin == "retired" or (different and choice == "retired"):
                merged[key] = retired_value
            key_rows.append({
                "key": key,
                "origin": origin,
                "conflict": different,
                "kept_value": kept_value if in_kept else None,
                "retired_value": retired_value if in_retired else None,
                "choice": choice if different else origin,
                "final_value": merged.get(key),
            })
        # 汇总全部别名到 passport._merged_aliases，便于下游识别旧称。
        all_aliases: list[Any] = []
        for key in sorted(set(kept_passport) | set(retired_passport), key=_norm_text):
            if _norm_text(key) in alias_keys:
                for value in _flatten_alias_values(kept_passport.get(key)) + _flatten_alias_values(retired_passport.get(key)):
                    if value not in all_aliases:
                        all_aliases.append(value)
        if all_aliases:
            merged["_merged_aliases"] = all_aliases
        if undecided:
            raise ConflictError("存在未逐项决定的护照冲突字段", context={"conflicting_passport_keys": undecided})
        used = {row["key"] for row in key_rows if row["conflict"]}
        invalid = {key for key, value in normalized_decisions.items()
                   if key not in {_norm_text(name) for name in used}}
        if invalid:
            raise ValidationError(f"护照保留决定引用了不存在或无冲突的字段: {sorted(invalid)}")
        return {"strategy": "union", "merged": merged, "key_decisions": key_rows}

    def _merge_restrictions(self, kept: dict[str, Any], retired: dict[str, Any]) -> dict[str, Any]:
        kept_source = self._source_for(kept)
        retired_source = self._source_for(retired)
        kept_source_rules = kept_source.get("restrictions", {}) if kept_source else {}
        retired_source_rules = retired_source.get("restrictions", {}) if retired_source else {}
        kept_rules = kept.get("passport", {}).get("restrictions", {})
        retired_rules = retired.get("passport", {}).get("restrictions", {})
        # 安全归并：双方来源级与护照级限制全部并入保留档案，取最严格并集，避免合并后限制失效。
        merged_rules = _union_restrictions(
            _union_restrictions(kept_source_rules, retired_source_rules),
            _union_restrictions(kept_rules, retired_rules),
        )
        return {
            "kept_source": kept_source,
            "retired_source": retired_source,
            "kept_source_restrictions": kept_source_rules,
            "retired_source_restrictions": retired_source_rules,
            "kept_passport_restrictions": kept_rules,
            "retired_passport_restrictions": retired_rules,
            "merged_passport_restrictions": merged_rules,
            "distribution_allowed_after": (
                kept["status"] == "accepted" or retired["status"] == "accepted"
            ) and not merged_rules.get("no_distribution", False),
        }

    def _source_for(self, accession: dict[str, Any]) -> dict[str, Any] | None:
        source_id = accession.get("source_id")
        if not source_id:
            return None
        return record(self.connection.execute("SELECT * FROM collection_sources WHERE id=?", (source_id,)).fetchone())

    def _preview_closure(self, kept_id: int, retired_id: int) -> dict[str, Any]:
        """预演时给出链式收敛范围：所有已指向退休档案的别名/档案将被扁平改指向保留档案。"""
        alias_rows = self.connection.execute(
            "SELECT accession_no FROM accession_aliases WHERE accession_id=? ORDER BY accession_no", (retired_id,)
        ).fetchall()
        dependent_rows = self.connection.execute(
            "SELECT id,accession_no FROM accessions WHERE merged_into_accession_id=? ORDER BY id", (retired_id,)
        ).fetchall()
        return {
            "flatten_aliases": [row[0] for row in alias_rows],
            "flatten_dependents": [{"accession_id": row[0], "accession_no": row[1]} for row in dependent_rows],
        }

    def _plan_fingerprint(self, plan: dict[str, Any]) -> str:
        from app.core.security import request_fingerprint

        return request_fingerprint({
            "scalar_decisions": plan["scalar_decisions"],
            "passport": plan["passport"],
            "restrictions": plan["restrictions"],
        })

    def _build_audit_reason(
        self,
        candidate: dict[str, Any],
        kept: dict[str, Any],
        retired: dict[str, Any],
        plan: dict[str, Any],
        fingerprint: str,
        actor: str,
        timestamp: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        conflicts = [
            {"field": row["field"], "kept_value": row["kept_value"], "retired_value": row["retired_value"], "choice": row["choice"]}
            for row in plan["scalar_decisions"] if row["conflict"]
        ]
        passport_conflicts = [
            {"key": row["key"], "kept_value": row["kept_value"], "retired_value": row["retired_value"], "choice": row["choice"]}
            for row in plan["passport"]["key_decisions"] if row["conflict"]
        ]
        return {
            "rule_version": candidate["rule_version"],
            "candidate_id": candidate["id"],
            "candidate_version": candidate["version"],
            "score": candidate["score"],
            "evidence": candidate["evidence"],
            "actor": actor,
            "reason": reason,
            "created_at": timestamp,
            "kept_accession_id": kept["id"],
            "kept_accession_no": kept["accession_no"],
            "retired_accession_id": retired["id"],
            "retired_accession_no": retired["accession_no"],
            "scalar_conflicts": conflicts,
            "passport_conflicts": passport_conflicts,
            "restrictions": plan["restrictions"],
            "plan_fingerprint": fingerprint,
        }

    # ------------------------------------------------------------------ 执行

    def execute_merge(self, merge_id: int, data: dict[str, Any]) -> dict[str, Any]:
        idempotency_key = str(data["idempotency_key"])
        replayed = self._replay_by_key(idempotency_key, expect_merge_id=merge_id)
        if replayed is not None:
            return replayed | {"replayed": True}
        merge = self._require_merge(merge_id)
        if merge["status"] != "previewed":
            raise ConflictError("合并已经执行，重复请求必须使用原幂等键")
        candidate = self._require_candidate(int(merge["candidate_id"]))
        if int(candidate["version"]) != int(data["expected_version"]):
            raise ConflictError("候选记录版本与请求不一致，请基于最新候选重新预演", context={
                "current_version": candidate["version"]
            })
        if int(merge["candidate_version"]) != int(candidate["version"]):
            raise ConflictError("候选在预演后发生变化，请重新预演", context={
                "preview_version": merge["candidate_version"], "current_version": candidate["version"]
            })
        if candidate["status"] not in {"open", "deferred"}:
            raise ConflictError("候选已被终判，不能执行合并", context={"status": candidate["status"]})
        kept = self.repository.require_accession(int(merge["kept_accession_id"]))
        retired = self.repository.require_accession(int(merge["retired_accession_id"]))
        self._assert_snapshots_current(candidate, [kept, retired])
        self._guard_mergeable(kept, retired)
        self._assert_no_pending_distributions(int(merge["retired_accession_id"]))
        other = self.connection.execute(
            "SELECT id FROM accession_merges WHERE idempotency_key=? AND id<>?", (idempotency_key, merge_id)
        ).fetchone()
        if other is not None:
            raise ConflictError("同一幂等键已经用于其他合并", context={"merge_id": int(other[0])})

        self._apply_merge(merge, candidate, kept, retired, data["actor"], idempotency_key, to_storage(self.clock.now()))
        return self.merge_detail(merge_id, include_graphs=True) | {"replayed": False}

    def _replay_by_key(self, key: str, *, expect_merge_id: int) -> dict[str, Any] | None:
        existing = mrecord(self.connection.execute(
            "SELECT * FROM accession_merges WHERE idempotency_key=?", (key,)
        ).fetchone())
        if existing is None:
            return None
        if int(existing["id"]) != expect_merge_id:
            raise ConflictError("同一幂等键已经用于其他合并", context={"merge_id": existing["id"]})
        if existing["status"] != "completed":
            return None
        return self.merge_detail(expect_merge_id, include_graphs=True)

    def _require_merge(self, merge_id: int) -> dict[str, Any]:
        merge = mrecord(self.connection.execute(
            "SELECT * FROM accession_merges WHERE id=?", (merge_id,)
        ).fetchone())
        if merge is None:
            raise NotFoundError("合并预演记录不存在")
        return merge

    def _guard_mergeable(self, kept: dict[str, Any], retired: dict[str, Any]) -> None:
        if int(kept["id"]) == int(retired["id"]):
            raise ValidationError("不能将档案与自身合并")
        if kept["status"] == "merged" or kept.get("merged_into_accession_id") is not None:
            raise ConflictError("保留档案已在合并链中冻结，禁止合并", context={"accession_id": kept["id"]})
        if retired["status"] == "merged" or retired.get("merged_into_accession_id") is not None:
            raise ConflictError("退休档案已在合并链中冻结，禁止循环或重复合并", context={"accession_id": retired["id"]})
        if self._was_kept_before(int(retired["id"])):
            raise ConflictError("退休档案曾作为保留根完成过合并，禁止链式合并", context={"accession_id": retired["id"]})
        if self._resolve_root(int(kept["id"])) != int(kept["id"]) or self._resolve_root(int(retired["id"])) != int(retired["id"]):
            raise ConflictError("档案已经被别名指向其他保留档案，禁止链式合并")

    def _resolve_root(self, accession_id: int) -> int:
        current = accession_id
        seen = {current}
        for _ in range(MAX_MERGE_DEPTH + 1):
            row = self.connection.execute(
                "SELECT merged_into_accession_id FROM accessions WHERE id=?", (current,)
            ).fetchone()
            if row is None or row[0] is None:
                return current
            current = int(row[0])
            if current in seen:
                raise ConflictError("检测到循环合并链，已中止")
            seen.add(current)
        raise ConflictError("合并链深度超过安全限制，疑似循环合并")

    def _apply_merge(
        self,
        merge: dict[str, Any],
        candidate: dict[str, Any],
        kept: dict[str, Any],
        retired: dict[str, Any],
        actor: str,
        idempotency_key: str,
        timestamp: str,
    ) -> None:
        kept_id, retired_id = int(kept["id"]), int(retired["id"])
        plan = merge["plan"]

        # 1. 种子批次安全改挂保留档案；检测任务、计数、复检日程、移动/摆放记录经由批次自动归属，全程不改。
        moved_lots = records(self.connection.execute(
            "SELECT id,lot_no FROM seed_lots WHERE accession_id=? ORDER BY id", (retired_id,)
        ).fetchall())
        self.connection.execute(
            "UPDATE seed_lots SET accession_id=?,version=version+1,updated_at=? WHERE accession_id=?",
            (kept_id, timestamp, retired_id),
        )

        # 2. 事件历史归并：旧档案事件原样保留，复制带溯源的 merged_history 事件到保留档案。
        retired_events = records(self.connection.execute(
            "SELECT * FROM accession_events WHERE accession_id=? ORDER BY id", (retired_id,)
        ).fetchall())
        for event in retired_events:
            detail = dict(event.get("detail", {}))
            detail.update({
                "merged_from_accession_id": retired_id,
                "merged_from_accession_no": retired["accession_no"],
                "original_event_id": event["id"],
                "original_event_type": event["event_type"],
                "original_created_at": event["created_at"],
                "merge_no": merge["merge_no"],
            })
            self.connection.execute(
                "INSERT INTO accession_events(accession_id,event_type,actor,from_status,to_status,detail_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (kept_id, "merged_history", event["actor"], event.get("from_status"), event.get("to_status"),
                 json.dumps(detail, ensure_ascii=False, sort_keys=True), timestamp),
            )

        # 3. 保留档案按预演中的逐项决定落字段；护照别名与限制归并结果直接取自预演。
        final_scalars = plan["final_scalars"]
        merged_passport = dict(plan["passport"]["merged"])
        merged_rules = plan["restrictions"]["merged_passport_restrictions"]
        if merged_rules:
            merged_passport["restrictions"] = merged_rules
        self.connection.execute(
            "UPDATE accessions SET scientific_name=?,crop_name=?,cultivar_name=?,source_id=?,acquisition_type=?,"
            "received_on=?,passport_json=?,version=version+1,updated_at=? WHERE id=?",
            (
                final_scalars["scientific_name"], final_scalars["crop_name"], final_scalars["cultivar_name"],
                final_scalars["source_id"], final_scalars["acquisition_type"], final_scalars["received_on"],
                json.dumps(merged_passport, ensure_ascii=False, sort_keys=True), timestamp, kept_id,
            ),
        )

        # 4. 旧档案冻结为 merged，原值与历史全部保留供不可变记录继续引用。
        self.connection.execute(
            "UPDATE accessions SET status='merged',merged_into_accession_id=?,version=version+1,updated_at=? WHERE id=?",
            (kept_id, timestamp, retired_id),
        )
        self._event(kept_id, "merged_in", actor, kept["status"], kept["status"], {
            "merge_no": merge["merge_no"],
            "merged_from_accession_id": retired_id,
            "merged_from_accession_no": retired["accession_no"],
            "moved_lot_ids": [lot["id"] for lot in moved_lots],
        }, timestamp)
        self._event(retired_id, "merged_away", actor, retired["status"], "merged", {
            "merge_no": merge["merge_no"],
            "merged_into_accession_id": kept_id,
            "merged_into_accession_no": kept["accession_no"],
        }, timestamp)

        # 5. 旧资源号登记别名并直接指向最终保留档案；历史别名与既有依赖闭包扁平化，杜绝循环/链式解析。
        self.connection.execute(
            "INSERT INTO accession_aliases(accession_no,accession_id,merge_id,created_at) VALUES(?,?,?,?)",
            (retired["accession_no"], kept_id, int(merge["id"]), timestamp),
        )
        self.connection.execute(
            "UPDATE accession_aliases SET accession_id=? WHERE accession_id=?", (kept_id, retired_id)
        )
        flattened = self.connection.execute(
            "SELECT id,accession_no FROM accessions WHERE merged_into_accession_id=? ORDER BY id", (retired_id,)
        ).fetchall()
        self.connection.execute(
            "UPDATE accessions SET merged_into_accession_id=? WHERE merged_into_accession_id=?", (kept_id, retired_id)
        )
        for row in flattened:
            self._event(int(row[0]), "merge_chain_flattened", actor, "merged", "merged", {
                "merge_no": merge["merge_no"], "new_target_accession_id": kept_id,
                "new_target_accession_no": kept["accession_no"],
            }, timestamp)

        # 6. 已签发发放记录保持不可变：明细仍指向冻结档案，仅登记到审计理由与后置图；检测结果随批次自动归属且不改动。
        immutable_distributions = records(self.connection.execute(
            "SELECT i.id AS item_id,i.request_id,i.accession_id,i.status AS item_status,r.request_no,r.status AS request_status "
            "FROM distribution_items i JOIN distribution_requests r ON r.id=i.request_id WHERE i.accession_id=? ORDER BY i.id",
            (retired_id,),
        ).fetchall())

        # 7. 其余与退休档案相关的未决候选收敛，防止同一对档案再次进入合并。
        self.connection.execute(
            "UPDATE duplicate_candidates SET status='superseded',decision_reason=?,updated_at=? "
            "WHERE id<>? AND status IN ('open','deferred') AND (left_accession_id=? OR right_accession_id=?)",
            (
                f"档案 {retired['accession_no']} 已并入 {kept['accession_no']}（{merge['merge_no']}）",
                timestamp, int(candidate["id"]), retired_id, retired_id,
            ),
        )

        # 8. 合并台账落账：幂等键、后置引用图、状态翻转。
        after_graph = {"kept": self.build_graph(kept_id), "retired": self.build_graph(retired_id)}
        self.connection.execute(
            "UPDATE accession_merges SET status='completed',idempotency_key=?,after_graph_json=?,executed_by=?,"
            "executed_at=?,updated_at=? WHERE id=?",
            (
                idempotency_key, json.dumps(after_graph, ensure_ascii=False, sort_keys=True),
                actor, timestamp, timestamp, int(merge["id"]),
            ),
        )
        self.connection.execute(
            "UPDATE duplicate_candidates SET status='merge_decided',merge_id=?,decided_by=?,decided_at=?,"
            "version=version+1,updated_at=? WHERE id=?",
            (int(merge["id"]), actor, timestamp, timestamp, int(candidate["id"])),
        )
        self._append_merge_event(int(merge["id"]), "completed", {
            "by": actor,
            "moved_lot_ids": [lot["id"] for lot in moved_lots],
            "copied_event_count": len(retired_events),
            "flattened_dependents": [{"accession_id": row[0], "accession_no": row[1]} for row in flattened],
            "immutable_distribution_items": immutable_distributions,
        }, timestamp)
        self.connection.execute(
            "INSERT INTO outbox_events(event_key,event_type,aggregate_type,aggregate_id,payload_json,available_at,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                f"accession-merged-{merge['id']}", "accession.merged", "accession", str(kept_id),
                json.dumps({
                    "merge_no": merge["merge_no"], "kept_accession_id": kept_id,
                    "retired_accession_id": retired_id, "retired_accession_no": retired["accession_no"],
                    "moved_lot_ids": [lot["id"] for lot in moved_lots],
                }, ensure_ascii=False),
                timestamp, timestamp,
            ),
        )
        self._append_audit("duplicate.merge.execute", actor, "accession_merge", int(merge["id"]), {
            "merge_no": merge["merge_no"],
            "candidate_id": int(candidate["id"]),
            "kept_accession_id": kept_id,
            "kept_accession_no": kept["accession_no"],
            "retired_accession_id": retired_id,
            "retired_accession_no": retired["accession_no"],
            "moved_lot_ids": [lot["id"] for lot in moved_lots],
            "copied_event_count": len(retired_events),
            "flattened_dependents": [{"accession_id": row[0], "accession_no": row[1]} for row in flattened],
            "immutable_distribution_items": immutable_distributions,
            "idempotency_key": idempotency_key,
        }, timestamp)

    # ------------------------------------------------------------------ 台账与图

    def list_merges(self, *, status: str | None = None, accession_id: int | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            if status not in {"previewed", "completed"}:
                raise ValidationError("合并状态无效")
            where.append("status=?")
            params.append(status)
        if accession_id is not None:
            where.append("(kept_accession_id=? OR retired_accession_id=?)")
            params.extend([accession_id, accession_id])
        clause = " WHERE " + " AND ".join(where) if where else ""
        params.append(200)
        return mrecords(self.connection.execute(
            f"SELECT * FROM accession_merges{clause} ORDER BY id DESC LIMIT ?", params
        ).fetchall())

    def merge_detail(self, merge_id: int, *, include_graphs: bool = True) -> dict[str, Any]:
        merge = self._require_merge(merge_id)
        merge["kept_accession"] = self.repository.accession_detail(int(merge["kept_accession_id"]))
        merge["retired_accession"] = self.repository.accession_detail(int(merge["retired_accession_id"]))
        merge["merge_events"] = records(self.connection.execute(
            "SELECT * FROM accession_merge_events WHERE merge_id=? ORDER BY id", (merge_id,)
        ).fetchall())
        merge["aliases"] = records(self.connection.execute(
            "SELECT * FROM accession_aliases WHERE merge_id=? ORDER BY accession_no", (merge_id,)
        ).fetchall())
        if not include_graphs:
            merge.pop("before_graph", None)
            merge.pop("after_graph", None)
        return merge

    def merge_graph(self, merge_id: int, phase: str) -> dict[str, Any]:
        merge = self._require_merge(merge_id)
        if phase == "before":
            graph = merge["before_graph"]
        elif phase == "after":
            if not merge.get("after_graph"):
                raise ConflictError("合并尚未执行，后置引用图还不可用")
            graph = merge["after_graph"]
        else:
            raise ValidationError("引用图阶段必须是 before 或 after")
        return {"merge_id": merge_id, "merge_no": merge["merge_no"], "phase": phase, "graph": graph}

    def build_graph(self, accession_id: int) -> dict[str, Any]:
        accession = self.repository.require_accession(accession_id)
        source = None
        if accession.get("source_id"):
            source = record(self.connection.execute(
                "SELECT * FROM collection_sources WHERE id=?", (accession["source_id"],)
            ).fetchone())
        lots = records(self.connection.execute(
            "SELECT * FROM seed_lots WHERE accession_id=? ORDER BY id", (accession_id,)
        ).fetchall())
        for lot in lots:
            lot_id = int(lot["id"])
            lot["placements"] = records(self.connection.execute(
                "SELECT * FROM lot_placements WHERE lot_id=? ORDER BY id", (lot_id,)
            ).fetchall())
            lot["movements"] = records(self.connection.execute(
                "SELECT id,movement_type,quantity_grams,idempotency_key,created_at FROM lot_movements WHERE lot_id=? ORDER BY id",
                (lot_id,),
            ).fetchall())
            lot["holds"] = records(self.connection.execute(
                "SELECT id,hold_type,reason,imposed_at,released_at FROM lot_holds WHERE lot_id=? ORDER BY id", (lot_id,)
            ).fetchall())
            lot["tests"] = records(self.connection.execute(
                "SELECT id,test_no,status,germination_percent,vigor_index,completed_at,version FROM viability_tests WHERE lot_id=? ORDER BY id",
                (lot_id,),
            ).fetchall())
            lot["schedules"] = records(self.connection.execute(
                "SELECT id,due_on,status FROM retest_schedules WHERE lot_id=? ORDER BY id", (lot_id,)
            ).fetchall())
        events = records(self.connection.execute(
            "SELECT id,event_type,actor,from_status,to_status,detail_json,created_at FROM accession_events WHERE accession_id=? ORDER BY id",
            (accession_id,),
        ).fetchall())
        distribution_items = records(self.connection.execute(
            "SELECT i.id,i.request_id,i.quantity_grams,i.status,i.allocated_lot_id,r.request_no,r.status AS request_status "
            "FROM distribution_items i JOIN distribution_requests r ON r.id=i.request_id WHERE i.accession_id=? ORDER BY i.id",
            (accession_id,),
        ).fetchall())
        aliases = records(self.connection.execute(
            "SELECT accession_no,accession_id FROM accession_aliases WHERE accession_id=? ORDER BY accession_no", (accession_id,)
        ).fetchall())
        merged_into = None
        if accession.get("merged_into_accession_id"):
            target = self.repository.require_accession(int(accession["merged_into_accession_id"]))
            merged_into = {"accession_id": target["id"], "accession_no": target["accession_no"]}
        return {
            "accession": {key: accession[key] for key in (
                "id", "accession_no", "status", "version", "scientific_name", "crop_name",
                "cultivar_name", "source_id", "merged_into_accession_id",
            )},
            "source": source,
            "merged_into": merged_into,
            "aliases": aliases,
            "lots": lots,
            "events": events,
            "distribution_items": distribution_items,
        }

    # ------------------------------------------------------------------ 编号解析

    def resolve_accession_no(self, accession_no: str) -> dict[str, Any]:
        accession = self.repository.accession_by_number(accession_no.strip())
        if accession is not None:
            root_id = self._resolve_root(int(accession["id"]))
            result = self.repository.accession_detail(root_id)
            result["resolution"] = "direct" if root_id == int(accession["id"]) else "merged_redirect"
            result["resolved_from_accession_no"] = accession_no
            result["requested_accession"] = {
                "id": accession["id"], "accession_no": accession["accession_no"], "status": accession["status"],
            }
            return result
        alias = record(self.connection.execute(
            "SELECT * FROM accession_aliases WHERE accession_no=?", (accession_no.strip(),)
        ).fetchone())
        if alias is None:
            raise NotFoundError("资源编号及其合并别名均不存在")
        root_id = self._resolve_root(int(alias["accession_id"]))
        result = self.repository.accession_detail(root_id)
        result["resolution"] = "alias"
        result["resolved_from_accession_no"] = accession_no
        return result

    # ------------------------------------------------------------------ 辅助

    def _event(
        self, accession_id: int, event_type: str, actor: str,
        from_status: str | None, to_status: str | None, detail: dict[str, Any], timestamp: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO accession_events(accession_id,event_type,actor,from_status,to_status,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (accession_id, event_type, actor, from_status, to_status,
             json.dumps(detail, ensure_ascii=False, sort_keys=True), timestamp),
        )

    def _append_merge_event(self, merge_id: int, event_type: str, detail: dict[str, Any], timestamp: str) -> None:
        self.connection.execute(
            "INSERT INTO accession_merge_events(merge_id,event_type,detail_json,created_at) VALUES(?,?,?,?)",
            (merge_id, event_type, json.dumps(detail, ensure_ascii=False, sort_keys=True), timestamp),
        )

    def _append_audit(
        self,
        action: str,
        actor: str,
        resource_type: str,
        resource_id: int | None,
        metadata: dict[str, Any],
        timestamp: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(actor_name,action,resource_type,resource_id,outcome,metadata_json,created_at) "
            "VALUES(?,?,?,?, 'success',?,?)",
            (
                actor, action, resource_type, str(resource_id) if resource_id is not None else None,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True), timestamp,
            ),
        )
