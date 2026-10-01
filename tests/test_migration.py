from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


OLD_ACCESSIONS_DDL = """
CREATE TABLE accessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_no TEXT NOT NULL UNIQUE,
    scientific_name TEXT NOT NULL,
    crop_name TEXT NOT NULL,
    cultivar_name TEXT NOT NULL DEFAULT '',
    source_id INTEGER,
    acquisition_type TEXT NOT NULL CHECK(acquisition_type IN ('采集','引进','交换','捐赠','育种')),
    received_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','quarantine','accepted','restricted','retired')),
    quarantine_reason TEXT NOT NULL DEFAULT '',
    passport_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE seed_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_no TEXT NOT NULL UNIQUE,
    accession_id INTEGER NOT NULL REFERENCES accessions(id) ON DELETE RESTRICT,
    parent_lot_id INTEGER REFERENCES seed_lots(id),
    harvest_year INTEGER NOT NULL,
    initial_weight_grams REAL NOT NULL,
    available_weight_grams REAL NOT NULL,
    moisture_percent REAL,
    treatment TEXT NOT NULL DEFAULT '',
    sealed_on TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@pytest.fixture()
def legacy_db(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(OLD_ACCESSIONS_DDL)
    connection.execute(
        "INSERT INTO accessions(accession_no,scientific_name,crop_name,acquisition_type,received_on,status,"
        "passport_json,created_by,created_at,updated_at) VALUES('LEGACY-1','Oryza sativa','水稻','采集',"
        "'2019-01-01','accepted','{\"locality\": \"旧档地点\"}','旧系统','2019-01-01T00:00:00+00:00','2019-01-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO seed_lots(lot_no,accession_id,harvest_year,initial_weight_grams,available_weight_grams,"
        "created_by,created_at,updated_at) VALUES('LOT-LEGACY-1',1,2018,500,500,'旧系统',"
        "'2019-01-02T00:00:00+00:00','2019-01-02T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()
    return db_path


def test_legacy_database_migrates_with_data_and_references_intact(legacy_db: Path, monkeypatch):
    from app.database import close_connection, get_connection, init_db

    monkeypatch.setenv("GERMPLASM_DATABASE_PATH", str(legacy_db))
    close_connection()
    init_db()
    connection = get_connection()

    columns = [item[1] for item in connection.execute("PRAGMA table_info(accessions)").fetchall()]
    assert "merged_into_accession_id" in columns

    # 旧数据完整保留
    record = connection.execute(
        "SELECT accession_no,scientific_name,status,merged_into_accession_id FROM accessions WHERE accession_no='LEGACY-1'"
    ).fetchone()
    assert record[0] == "LEGACY-1"
    assert record[2] == "accepted"
    assert record[3] is None

    # 批次外键引用仍有效
    lot = connection.execute("SELECT accession_id FROM seed_lots WHERE lot_no='LOT-LEGACY-1'").fetchone()
    assert lot[0] == 1

    # 新表就位
    tables = {item[0] for item in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert {"duplicate_candidates", "accession_merges", "accession_aliases"} <= tables

    # 外键校验无错误；CHECK 已允许 merged 状态
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.execute("UPDATE accessions SET status='merged',merged_into_accession_id=id WHERE accession_no='LEGACY-1'")
    connection.commit()
    close_connection()


def test_migration_is_idempotent(legacy_db: Path, monkeypatch):
    from app.database import close_connection, init_db

    monkeypatch.setenv("GERMPLASM_DATABASE_PATH", str(legacy_db))
    close_connection()
    init_db()
    init_db()  # 第二次运行不应报错或重复改动
    close_connection()
