from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import request_fingerprint
from app.germplasm.repository import GermplasmRepository, record, records

RULESET_VERSION = "dup-rules-v1"
CANDIDATE_THRESHOLD = 0.5
CORE_FIELDS = ("scientific_name", "crop_name", "cultivar_name", "source_id")
REVIEWABLE_STATUSES = ("pending", "deferred")


def _normalize(value: Any) -> str:
    return " ".join(str(value if value is not None else "").split()).casefold()


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _merge_restrictions(survivor_rules: dict[str, Any], retired_rules: dict[str, Any]) -> tuple[dict[str, Any], list[dict]]:
    """按“更严格一方生效”合并限制字典，布尔冲突取真，其余冲突保留保留档案的值。"""
    merged = dict(survivor_rules)
    conflicts: list[dict] = []
    for key in sorted(retired_rules):
        if key not in merged or merged[key] == retired_rules[key]:
            merged.setdefault(key, retired_rules[key])
            continue
        if isinstance(merged[key], bool) and isinstance(retired_rules[key], bool):
            merged[key] = bool(merged[key] or retired_rules[key])
            conflicts.append({"key": key, "resolution": "most_restrictive"})
        else:
            conflicts.append({
                "key": key, "resolution": "kept_survivor",
                "survivor_value": merged[key], "retired_value": retired_rules[key],
            })
    return merged, conflicts


class DuplicateService:
    """疑似重复识别与人工合并：规则只生成候选，合并由审阅人确认后安全归并。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    # ---------- 识别 ----------

    def scan(self) -> dict[str, Any]:
        rows = records(self.connection.execute(
            "SELECT * FROM accessions WHERE merged_into_id IS NULL AND status<>'retired' ORDER BY id"
        ).fetchall())
        sources = {
            int(item["id"]): item
            for item in records(self.connection.execute("SELECT * FROM collection_sources").fetchall())
        }
        timestamp = to_storage(self.clock.now())
        created = updated = unchanged = skipped = 0
        candidate_ids: list[int] = []
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                score, evidence = self._score_pair(
                    left, right, sources.get(left.get("source_id")), sources.get(right.get("source_id"))
                )
                if score < CANDIDATE_THRESHOLD:
                    continue
                a_id, b_id = sorted((int(left["id"]), int(right["id"])))
                key = f"dup:{a_id}:{b_id}"
                payload = _dump({"rules": evidence})
                existing = self.repository.candidate_by_key(key)
                if existing is None:
                    cursor = self.connection.execute(
                        "INSERT INTO duplicate_candidates(candidate_key,accession_a_id,accession_b_id,score,"
                        "ruleset_version,evidence_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
                        (key, a_id, b_id, score, RULESET_VERSION, payload, timestamp, timestamp),
                    )
                    created += 1
                    candidate_ids.append(int(cursor.lastrowid))
                    continue
                if existing["status"] not in REVIEWABLE_STATUSES:
                    skipped += 1
                    continue
                same_evidence = _dump(existing.get("evidence", {})) == payload
                if (
                    abs(float(existing["score"]) - score) < 1e-9
                    and existing["ruleset_version"] == RULESET_VERSION
                    and same_evidence
                ):
                    unchanged += 1
                    candidate_ids.append(int(existing["id"]))
                    continue
                self.connection.execute(
                    "UPDATE duplicate_candidates SET score=?,ruleset_version=?,evidence_json=?,"
                    "version=version+1,updated_at=? WHERE id=?",
                    (score, RULESET_VERSION, payload, timestamp, existing["id"]),
                )
                updated += 1
                candidate_ids.append(int(existing["id"]))
        return {
            "ruleset_version": RULESET_VERSION,
            "threshold": CANDIDATE_THRESHOLD,
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "skipped": skipped,
            "candidate_ids": candidate_ids,
        }

    def _score_pair(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        source_left: dict[str, Any] | None,
        source_right: dict[str, Any] | None,
    ) -> tuple[float, list[dict[str, Any]]]:
        score = 0.0
        evidence: list[dict[str, Any]] = []

        def add(rule: str, weight: float, field: str, value_a: Any, value_b: Any) -> None:
            nonlocal score
            score = round(score + weight, 6)
            evidence.append({"rule": rule, "field": field, "weight": weight, "value_a": value_a, "value_b": value_b})

        if _normalize(left["scientific_name"]) and _normalize(left["scientific_name"]) == _normalize(right["scientific_name"]):
            add("scientific_name_exact", 0.45, "scientific_name", left["scientific_name"], right["scientific_name"])
        if _normalize(left["crop_name"]) and _normalize(left["crop_name"]) == _normalize(right["crop_name"]):
            add("crop_name_exact", 0.15, "crop_name", left["crop_name"], right["crop_name"])
        if _normalize(left.get("cultivar_name")) and _normalize(left["cultivar_name"]) == _normalize(right.get("cultivar_name")):
            add("cultivar_name_exact", 0.15, "cultivar_name", left["cultivar_name"], right["cultivar_name"])
        if left.get("source_id") and left.get("source_id") == right.get("source_id"):
            add("same_source", 0.15, "source_id", left["source_id"], right["source_id"])
        elif source_left and source_right:
            same_country = source_left["country_code"] == source_right["country_code"]
            same_locality = _normalize(source_left.get("locality")) and (
                _normalize(source_left["locality"]) == _normalize(source_right.get("locality"))
            )
            if same_country and same_locality:
                add("source_locality_exact", 0.10, "source.locality", source_left["locality"], source_right["locality"])
        passport_left = left.get("passport") or {}
        passport_right = right.get("passport") or {}
        matched_keys = 0
        for key in sorted((set(passport_left) & set(passport_right)) - {"restrictions"}):
            value_left, value_right = passport_left[key], passport_right[key]
            if isinstance(value_left, (dict, list)) or isinstance(value_right, (dict, list)):
                continue
            if _normalize(value_left) and _normalize(value_left) == _normalize(value_right):
                add("passport_field_match", 0.05, f"passport.{key}", value_left, value_right)
                matched_keys += 1
                if matched_keys >= 3:
                    break
        if left.get("received_on") and left.get("received_on") == right.get("received_on"):
            add("received_on_exact", 0.05, "received_on", left["received_on"], right["received_on"])
        return min(score, 1.0), evidence

    # ---------- 复核 ----------

    def review(self, candidate_id: int, data: dict[str, Any]) -> dict[str, Any]:
        candidate = self.repository.require_candidate(candidate_id)
        self._check_candidate_version(candidate, data["expected_version"])
        if candidate["status"] not in REVIEWABLE_STATUSES:
            raise ConflictError("候选已结案，不能复核", context={"status": candidate["status"]})
        action = data["action"]
        note = (data.get("note") or "").strip()
        if action == "dismiss" and not note:
            raise ValidationError("判定无关时必须填写复核说明")
        status = "dismissed" if action == "dismiss" else "deferred"
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE duplicate_candidates SET status=?,review_note=?,reviewed_by=?,reviewed_at=?,"
            "version=version+1,updated_at=? WHERE id=? AND version=?",
            (status, note, data["actor"], timestamp, timestamp, candidate_id, data["expected_version"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("候选版本冲突")
        return self.repository.require_candidate(candidate_id)

    def candidate_detail(self, candidate_id: int) -> dict[str, Any]:
        candidate = self.repository.require_candidate(candidate_id)
        candidate["accession_a"] = self.repository.require_accession(int(candidate["accession_a_id"]))
        candidate["accession_b"] = self.repository.require_accession(int(candidate["accession_b_id"]))
        candidate["merges"] = records(self.connection.execute(
            "SELECT id,merge_key,survivor_id,retired_id,actor,created_at FROM merge_records WHERE candidate_id=? ORDER BY id",
            (candidate_id,),
        ).fetchall())
        return candidate

    # ---------- 预演与合并 ----------

    def preview_merge(self, candidate_id: int, data: dict[str, Any]) -> dict[str, Any]:
        candidate = self.repository.require_candidate(candidate_id)
        self._check_candidate_version(candidate, data["expected_version"])
        survivor, retired = self._resolve_pair(candidate, int(data["survivor_id"]))
        plan = self._build_merge_plan(candidate, survivor, retired, data.get("field_decisions") or {})
        return {
            "dry_run": True,
            "candidate_id": candidate["id"],
            "candidate_version": candidate["version"],
            "survivor_id": survivor["id"],
            "retired_id": retired["id"],
            "decisions": plan["decisions"],
            "summary": plan["summary"],
            "before_graph": {
                "survivor": self._reference_graph(int(survivor["id"])),
                "retired": self._reference_graph(int(retired["id"])),
            },
        }

    def merge(self, candidate_id: int, data: dict[str, Any]) -> dict[str, Any]:
        candidate = self.repository.require_candidate(candidate_id)
        payload = {
            "candidate_id": candidate_id,
            "survivor_id": int(data["survivor_id"]),
            "field_decisions": data.get("field_decisions") or {},
            "reason": data["reason"],
            "actor": data["actor"],
        }
        fingerprint = request_fingerprint(payload)
        existing = self.repository.merge_by_key(data["idempotency_key"])
        if existing:
            if existing["request_hash"] != fingerprint:
                raise ConflictError("同一幂等键不能用于不同的合并请求")
            return {"merge": self.repository.merge_detail(int(existing["id"])), "replayed": True}
        self._check_candidate_version(candidate, data["expected_version"])
        if candidate["status"] not in REVIEWABLE_STATUSES:
            raise ConflictError("候选当前状态不能执行合并", context={"status": candidate["status"]})
        survivor, retired = self._resolve_pair(candidate, int(data["survivor_id"]))
        plan = self._build_merge_plan(candidate, survivor, retired, payload["field_decisions"])
        timestamp = to_storage(self.clock.now())
        before_graph = {
            "survivor": self._reference_graph(int(survivor["id"])),
            "retired": self._reference_graph(int(retired["id"])),
        }
        cursor = self.connection.execute(
            "INSERT INTO merge_records(merge_key,request_hash,candidate_id,survivor_id,retired_id,reason,actor,"
            "field_decisions_json,before_graph_json,after_graph_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,'{}',?)",
            (
                data["idempotency_key"], fingerprint, candidate["id"], survivor["id"], retired["id"],
                data["reason"], data["actor"], _dump(plan["decisions"]), _dump(before_graph), timestamp,
            ),
        )
        merge_id = int(cursor.lastrowid)
        for entry in plan["decisions"]:
            self.connection.execute(
                "INSERT INTO merge_field_decisions(merge_id,field_name,decision,survivor_value_json,"
                "retired_value_json,final_value_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    merge_id, entry["field"], entry["decision"], _dump(entry["survivor_value"]),
                    _dump(entry["retired_value"]), _dump(entry["final_value"]), timestamp,
                ),
            )
        self._apply_survivor_updates(survivor, plan, timestamp)
        retired_cursor = self.connection.execute(
            "UPDATE accessions SET status='retired',merged_into_id=?,merge_id=?,version=version+1,updated_at=? "
            "WHERE id=? AND merged_into_id IS NULL AND status<>'retired'",
            (survivor["id"], merge_id, timestamp, retired["id"]),
        )
        if retired_cursor.rowcount != 1:
            raise ConflictError("合并档案状态已变化，无法安全合并")
        self.connection.execute(
            "UPDATE seed_lots SET accession_id=?,version=version+1,updated_at=? WHERE accession_id=?",
            (survivor["id"], timestamp, retired["id"]),
        )
        self._copy_events(survivor, retired, merge_id)
        self._merge_event(
            int(survivor["id"]), "merged_duplicate", data["actor"], survivor["status"], survivor["status"],
            {"merge_id": merge_id, "retired_id": retired["id"], "retired_accession_no": retired["accession_no"],
             "reason": data["reason"]}, timestamp,
        )
        self._merge_event(
            int(retired["id"]), "retired_as_duplicate", data["actor"], retired["status"], "retired",
            {"merge_id": merge_id, "survivor_id": survivor["id"], "surviving_accession_no": survivor["accession_no"],
             "reason": data["reason"]}, timestamp,
        )
        for alias in plan["aliases"]:
            self.connection.execute(
                "INSERT INTO accession_aliases(accession_id,alias_type,alias_key,alias_value_json,merge_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (survivor["id"], alias["alias_type"], alias["alias_key"], _dump(alias["alias_value"]), merge_id, timestamp),
            )
        # 链式展平：此前并入“合并档案”的记录直接指向最终保留档案，避免 A→B→C 长链。
        self.connection.execute(
            "UPDATE accessions SET merged_into_id=? WHERE merged_into_id=?", (survivor["id"], retired["id"])
        )
        self.connection.execute(
            "UPDATE accession_aliases SET accession_id=? WHERE accession_id=?", (survivor["id"], retired["id"])
        )
        self.connection.execute(
            "UPDATE duplicate_candidates SET status='invalidated',review_note='关联档案已合并，候选失效',"
            "reviewed_by=?,reviewed_at=?,version=version+1,updated_at=? "
            "WHERE status IN ('pending','deferred') AND id<>? AND (accession_a_id=? OR accession_b_id=?)",
            (data["actor"], timestamp, timestamp, candidate["id"], retired["id"], retired["id"]),
        )
        candidate_cursor = self.connection.execute(
            "UPDATE duplicate_candidates SET status='merged',survivor_id=?,review_note=?,reviewed_by=?,reviewed_at=?,"
            "version=version+1,updated_at=? WHERE id=? AND version=?",
            (survivor["id"], data["reason"], data["actor"], timestamp, timestamp, candidate["id"], data["expected_version"]),
        )
        if candidate_cursor.rowcount != 1:
            raise ConflictError("候选版本冲突")
        after_graph = {
            "survivor": self._reference_graph(int(survivor["id"])),
            "retired": self._reference_graph(int(retired["id"])),
        }
        self.connection.execute(
            "UPDATE merge_records SET after_graph_json=? WHERE id=?", (_dump(after_graph), merge_id)
        )
        self.connection.execute(
            "INSERT INTO outbox_events(event_key,event_type,aggregate_type,aggregate_id,payload_json,available_at,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                f"accession-merged-{merge_id}", "accession.merged", "accession", str(survivor["id"]),
                json.dumps({"merge_id": merge_id, "candidate_id": candidate["id"], "survivor_id": survivor["id"],
                            "retired_id": retired["id"]}, ensure_ascii=False),
                timestamp, timestamp,
            ),
        )
        return {"merge": self.repository.merge_detail(merge_id), "replayed": False}

    def _resolve_pair(self, candidate: dict[str, Any], survivor_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        pair = {int(candidate["accession_a_id"]), int(candidate["accession_b_id"])}
        if survivor_id not in pair:
            raise ValidationError("保留档案必须是候选中的一方")
        retired_id = (pair - {survivor_id}).pop()
        survivor = self.repository.require_accession(survivor_id)
        retired = self.repository.require_accession(retired_id)
        for label, item in (("保留档案", survivor), ("合并档案", retired)):
            if item.get("merged_into_id"):
                raise ConflictError(
                    f"{label}已经并入其他档案，不能再次合并", context={"accession_id": item["id"]}
                )
            if item["status"] == "retired":
                raise ConflictError(f"{label}已退出保存，不能参与合并", context={"accession_id": item["id"]})
        return survivor, retired

    def _build_merge_plan(
        self,
        candidate: dict[str, Any],
        survivor: dict[str, Any],
        retired: dict[str, Any],
        overrides: dict[str, str],
    ) -> dict[str, Any]:
        for field, choice in overrides.items():
            if choice not in ("survivor", "retired"):
                raise ValidationError("字段决定只能是 survivor 或 retired", context={"field": field})
        decisions: list[dict[str, Any]] = []
        core_updates: dict[str, Any] = {}
        for field in CORE_FIELDS:
            survivor_value = survivor.get(field)
            retired_value = retired.get(field)
            if field == "source_id":
                survivor_value = int(survivor_value) if survivor_value is not None else None
                retired_value = int(retired_value) if retired_value is not None else None
            if survivor_value == retired_value:
                continue
            if _is_empty(retired_value):
                continue
            decision = "keep_retired" if _is_empty(survivor_value) and not _is_empty(retired_value) else "keep_survivor"
            if field in overrides:
                decision = "keep_survivor" if overrides[field] == "survivor" else "keep_retired"
            final_value = survivor_value if decision == "keep_survivor" else retired_value
            decisions.append({
                "field": field, "decision": decision, "survivor_value": survivor_value,
                "retired_value": retired_value, "final_value": final_value, "overridden": field in overrides,
            })
            if final_value != survivor_value:
                core_updates[field] = final_value
        survivor_passport = dict(survivor.get("passport") or {})
        retired_passport = dict(retired.get("passport") or {})
        final_passport = dict(survivor_passport)
        aliases: list[dict[str, Any]] = [{
            "alias_type": "accession_no",
            "alias_key": retired["accession_no"],
            "alias_value": {"retired_id": retired["id"], "surviving_accession_no": survivor["accession_no"]},
        }]
        for key in sorted((set(survivor_passport) | set(retired_passport)) - {"restrictions"}):
            field = f"passport.{key}"
            in_survivor = key in survivor_passport
            in_retired = key in retired_passport
            survivor_value = survivor_passport.get(key)
            retired_value = retired_passport.get(key)
            if in_survivor and in_retired and survivor_value == retired_value:
                continue
            if in_survivor and not in_retired:
                continue
            if in_survivor and in_retired and _is_empty(retired_value):
                continue
            if not in_survivor and _is_empty(retired_value):
                continue
            decision = "keep_retired" if not in_survivor else "keep_survivor"
            if field in overrides:
                decision = "keep_survivor" if overrides[field] == "survivor" else "keep_retired"
            final_value = survivor_value if decision == "keep_survivor" else retired_value
            if decision == "keep_retired":
                final_passport[key] = retired_value
            elif in_retired:
                aliases.append({"alias_type": "passport", "alias_key": key, "alias_value": retired_value})
            decisions.append({
                "field": field, "decision": decision,
                "survivor_value": survivor_value if in_survivor else None,
                "retired_value": retired_value if in_retired else None,
                "final_value": final_value, "overridden": field in overrides,
            })
        survivor_rules = survivor_passport.get("restrictions") or {}
        retired_rules = retired_passport.get("restrictions") or {}
        if survivor_rules or retired_rules:
            merged_rules, conflicts = _merge_restrictions(survivor_rules, retired_rules)
            if merged_rules != survivor_rules or conflicts:
                decisions.append({
                    "field": "passport.restrictions", "decision": "combined",
                    "survivor_value": survivor_rules, "retired_value": retired_rules,
                    "final_value": merged_rules, "overridden": False, "conflicts": conflicts,
                })
                final_passport["restrictions"] = merged_rules
        known_fields = {entry["field"] for entry in decisions if entry["decision"] != "combined"}
        unknown = sorted(set(overrides) - known_fields)
        if unknown:
            raise ValidationError("存在无效的字段决定", context={"fields": unknown})
        final_source_id = core_updates.get("source_id", survivor.get("source_id"))
        final_source_id = int(final_source_id) if final_source_id is not None else None
        retired_source_id = int(retired["source_id"]) if retired.get("source_id") is not None else None
        if retired_source_id is not None and retired_source_id != final_source_id:
            source = self.repository.require_source(retired_source_id)
            aliases.append({"alias_type": "source", "alias_key": source["source_code"], "alias_value": source})
        lots = records(self.connection.execute(
            "SELECT id,lot_no,status,available_weight_grams FROM seed_lots WHERE accession_id=? ORDER BY id",
            (retired["id"],),
        ).fetchall())
        events_count = int(self.connection.execute(
            "SELECT COUNT(*) FROM accession_events WHERE accession_id=?", (retired["id"],)
        ).fetchone()[0])
        distributions_count = int(self.connection.execute(
            "SELECT COUNT(*) FROM distribution_items WHERE accession_id=?", (retired["id"],)
        ).fetchone()[0])
        other_candidates = records(self.connection.execute(
            "SELECT id,candidate_key FROM duplicate_candidates WHERE status IN ('pending','deferred') AND id<>? "
            "AND (accession_a_id=? OR accession_b_id=?) ORDER BY id",
            (candidate["id"], retired["id"], retired["id"]),
        ).fetchall())
        incoming = records(self.connection.execute(
            "SELECT id,accession_no FROM accessions WHERE merged_into_id=? ORDER BY id", (retired["id"],)
        ).fetchall())
        passport_changed = final_passport != survivor_passport
        return {
            "decisions": decisions,
            "core_updates": core_updates,
            "final_passport": final_passport,
            "passport_changed": passport_changed,
            "aliases": aliases,
            "lots": lots,
            "summary": {
                "survivor_field_updates": {
                    field: {"from": survivor.get(field), "to": value} for field, value in core_updates.items()
                },
                "passport_changed": passport_changed,
                "lots_to_move": [{"id": lot["id"], "lot_no": lot["lot_no"]} for lot in lots],
                "events_to_copy": events_count,
                "aliases_to_create": [
                    {"alias_type": alias["alias_type"], "alias_key": alias["alias_key"]} for alias in aliases
                ],
                "distribution_items_kept_immutable": distributions_count,
                "candidates_to_invalidate": [item["id"] for item in other_candidates],
                "incoming_merges_to_repoint": incoming,
            },
        }

    def _apply_survivor_updates(self, survivor: dict[str, Any], plan: dict[str, Any], timestamp: str) -> None:
        assignments: list[str] = []
        params: list[Any] = []
        for field, value in plan["core_updates"].items():
            assignments.append(f"{field}=?")
            params.append(value)
        if plan["passport_changed"]:
            assignments.append("passport_json=?")
            params.append(_dump(plan["final_passport"]))
        if not assignments:
            return
        params.extend([timestamp, survivor["id"]])
        self.connection.execute(
            f"UPDATE accessions SET {','.join(assignments)},version=version+1,updated_at=? WHERE id=?", params
        )

    def _copy_events(self, survivor: dict[str, Any], retired: dict[str, Any], merge_id: int) -> None:
        events = records(self.connection.execute(
            "SELECT * FROM accession_events WHERE accession_id=? ORDER BY id", (retired["id"],)
        ).fetchall())
        for event in events:
            detail = dict(event.get("detail") or {})
            detail.update({
                "merged_from_accession_id": retired["id"],
                "original_event_id": event["id"],
                "merge_id": merge_id,
            })
            self.connection.execute(
                "INSERT INTO accession_events(accession_id,event_type,actor,from_status,to_status,detail_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    survivor["id"], event["event_type"], event["actor"], event["from_status"], event["to_status"],
                    json.dumps(detail, ensure_ascii=False, sort_keys=True), event["created_at"],
                ),
            )

    def _merge_event(
        self,
        accession_id: int,
        event_type: str,
        actor: str,
        from_status: str | None,
        to_status: str | None,
        detail: dict[str, Any],
        timestamp: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO accession_events(accession_id,event_type,actor,from_status,to_status,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (accession_id, event_type, actor, from_status, to_status,
             json.dumps(detail, ensure_ascii=False, sort_keys=True), timestamp),
        )

    # ---------- 解析与引用图 ----------

    def resolve_number(self, accession_no: str) -> dict[str, Any]:
        accession_no = accession_no.strip().upper()
        accession = self.repository.accession_by_number(accession_no)
        if accession is None:
            raise NotFoundError("种质资源编号不存在")
        chain: list[dict[str, Any]] = []
        current = accession
        seen = {int(current["id"])}
        while current.get("merged_into_id"):
            chain.append({
                "id": current["id"], "accession_no": current["accession_no"], "merge_id": current.get("merge_id"),
            })
            following = self.repository.require_accession(int(current["merged_into_id"]))
            if int(following["id"]) in seen:
                raise ConflictError("合并链存在异常循环", context={"accession_id": following["id"]})
            seen.add(int(following["id"]))
            current = following
        return {
            "accession_no": accession_no,
            "merged": bool(chain),
            "chain": chain,
            "resolved": self.repository.accession_detail(int(current["id"])),
        }

    def _reference_graph(self, accession_id: int) -> dict[str, Any]:
        accession = self.repository.require_accession(accession_id)
        core = {
            key: accession.get(key)
            for key in (
                "id", "accession_no", "scientific_name", "crop_name", "cultivar_name",
                "source_id", "status", "merged_into_id", "merge_id", "version",
            )
        }
        source = None
        if accession.get("source_id"):
            source = self.repository.require_source(int(accession["source_id"]))
        lots = records(self.connection.execute(
            "SELECT id,lot_no,status,available_weight_grams,harvest_year FROM seed_lots WHERE accession_id=? ORDER BY id",
            (accession_id,),
        ).fetchall())
        lot_ids = [int(lot["id"]) for lot in lots]
        tests: list[dict[str, Any]] = []
        placements: list[dict[str, Any]] = []
        holds: list[dict[str, Any]] = []
        schedules: list[dict[str, Any]] = []
        if lot_ids:
            marks = ",".join("?" for _ in lot_ids)
            tests = records(self.connection.execute(
                f"SELECT id,test_no,lot_id,test_type,status,germination_percent,completed_at FROM viability_tests "
                f"WHERE lot_id IN ({marks}) ORDER BY id", lot_ids,
            ).fetchall())
            placements = records(self.connection.execute(
                f"SELECT id,lot_id,location_id,container_code,weight_grams,removed_at FROM lot_placements "
                f"WHERE lot_id IN ({marks}) ORDER BY id", lot_ids,
            ).fetchall())
            holds = records(self.connection.execute(
                f"SELECT id,lot_id,hold_type,reason,imposed_at,released_at FROM lot_holds "
                f"WHERE lot_id IN ({marks}) ORDER BY id", lot_ids,
            ).fetchall())
            schedules = records(self.connection.execute(
                f"SELECT id,lot_id,due_on,status FROM retest_schedules WHERE lot_id IN ({marks}) ORDER BY id", lot_ids,
            ).fetchall())
        distributions = records(self.connection.execute(
            "SELECT i.id,i.request_id,i.quantity_grams,i.allocated_lot_id,i.status,r.request_no,r.status AS request_status "
            "FROM distribution_items i JOIN distribution_requests r ON r.id=i.request_id "
            "WHERE i.accession_id=? ORDER BY i.id", (accession_id,),
        ).fetchall())
        events = records(self.connection.execute(
            "SELECT id,event_type,actor,from_status,to_status,created_at FROM accession_events "
            "WHERE accession_id=? ORDER BY id", (accession_id,),
        ).fetchall())
        aliases = records(self.connection.execute(
            "SELECT id,alias_type,alias_key,alias_value_json,merge_id FROM accession_aliases "
            "WHERE accession_id=? ORDER BY id", (accession_id,),
        ).fetchall())
        merged_from = records(self.connection.execute(
            "SELECT id,accession_no,merge_id FROM accessions WHERE merged_into_id=? ORDER BY id", (accession_id,),
        ).fetchall())
        return {
            "accession": core,
            "source": source,
            "lots": lots,
            "viability_tests": tests,
            "placements": placements,
            "holds": holds,
            "retest_schedules": schedules,
            "distribution_items": distributions,
            "events": events,
            "aliases": aliases,
            "merged_from": merged_from,
        }

    @staticmethod
    def _check_candidate_version(candidate: dict[str, Any], expected_version: int) -> None:
        if int(candidate["version"]) != int(expected_version):
            raise ConflictError("候选版本冲突", context={"current_version": candidate["version"]})
