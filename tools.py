# -*- coding: utf-8 -*-
"""
tools.py — ローカルLLM(LM Studioなど)がThoughtsStoreを操作するためのツール関数群

save_thought / search_thoughts の2つを提供する。
書き込みロジックは既存の save_thought.py（thoughts_store_backend_lite）をそのまま再利用し、
二重管理を避ける。
"""
import json
import os
import sys
from typing import Optional, List, Dict

import jsonschema

# 分離版(llllm_memory_device)では save_thought.py / schema は同階層(フラット配置)にある
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import save_thought as st  # build_record, get_prev_hash, write_journal, update_indexes

SCHEMA_VERSION = "1.0"
_SCHEMA_PATH = os.path.join(_THIS_DIR, "events_ndjson_line.schema.json")
_schema_cache: Optional[Dict] = None


def _load_schema() -> Dict:
    """events_ndjson_line.schema.json をロードする（起動時Fail-Fastの簡易版）。

    「Schema検証は常に有効（任意適用という概念は無い）」という原則に従い、
    save_thought()は必ずこのスキーマでrecordを検証してから書き込む。
    """
    global _schema_cache
    if _schema_cache is None:
        if not os.path.exists(_SCHEMA_PATH):
            raise FileNotFoundError(f"スキーマファイルが見つかりません: {_SCHEMA_PATH}")
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            _schema_cache = json.load(f)
    return _schema_cache


def _validate_record(record: Dict) -> None:
    schema = _load_schema()
    record_schema = schema.get("properties", {}).get("record", {})
    jsonschema.validate(instance=record, schema=record_schema)


# thought の必須フィールド（extra からの上書きを禁止する集合）。
# type=="thought" / text必須 / tags最低1(最大5) / thread禁止 は不変の契約。
_PROTECTED_RECORD_KEYS = {"type", "text", "tags", "thread"}


def save_thought(mirror_base: str, text: str, tags: Optional[List[str]] = None,
                  author: str = "local-llm", extra: Optional[Dict] = None) -> Dict:
    """AI専用ストア(ai_ssot) に thought を1件追記する（save_thought.py と同じSSOT/ハッシュ連鎖ルールに従う）

    schema_version・authorをrecord内にセットし、書き込み前に必ずスキーマ検証を通す
    （Schema Enforcement 原則。AI専用ストアだからといって免除されない）。

    extra: record に載せる追加の構造化フィールド（例: {"json": {"image": {...}}}）。
      スキーマは record を additionalProperties:true で許容し、"json" は自由ペイロード
      （events_ndjson_line.schema.json）なので、画像参照などの束ね情報をここに入れられる。
      必須フィールド(type/text/tags/thread)は保護され、extra からは上書きできない
      （thought 契約を壊さない）。検証は従来どおり全レコードに強制される。
    """
    if not text or not text.strip():
        return {"status": "error", "message": "text が空です"}

    record = {"type": "thought", "text": text, "tags": tags or []}
    if extra:
        for k, v in extra.items():
            if k in _PROTECTED_RECORD_KEYS:
                continue  # 契約フィールドは保護（extra では触らせない）
            record[k] = v
    record.setdefault("schema_version", SCHEMA_VERSION)
    record.setdefault("author", author)

    try:
        _validate_record(record)
    except jsonschema.ValidationError as e:
        return {"status": "error", "message": f"スキーマ検証エラー: {e.message}"}

    with st.journal_lock(mirror_base):  # 排他制御(同一マシン内の競合防止)
        prev_hash = st.get_prev_hash(mirror_base)
        record_obj = st.build_record(author, prev_hash, record)
        filename, line_num = st.write_journal(mirror_base, record_obj)
        st.update_indexes(mirror_base, record_obj, filename, line_num)

    return {
        "status": "ok",
        "file": filename,
        "line": line_num,
        "hash": record_obj["hash"],
        "id": record_obj["id"],
    }


def search_thoughts(mirror_base: str, query: str = "", tag: str = "", limit: int = 5) -> List[Dict]:
    """indexes/locator.jsonl から text_head / tags を対象にキーワード検索する（新しい順）

    locator.jsonl は追記のみのインデックスなので、全文検索ではなく text_head（先頭80字）が
    対象になる点に注意。厳密な全文が必要な場合は source.file / source.line から journal_by_day
    の該当行を読み直す必要がある。
    """
    locator_path = os.path.join(mirror_base, "indexes", "locator.jsonl")
    if not os.path.exists(locator_path):
        return []

    query_lower = query.lower().strip()
    tag_lower = tag.lower().strip()
    hits: List[Dict] = []

    with open(locator_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for line in reversed(lines):  # 新しい順に走査
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        text_head = entry.get("text_head", "")
        entry_tags = [t.lower() for t in entry.get("tags", [])]

        if query_lower and query_lower not in text_head.lower():
            continue
        if tag_lower and tag_lower not in entry_tags:
            continue

        hits.append({
            "id": entry.get("id"),
            "date": entry.get("date_local"),
            "tags": entry.get("tags", []),
            "text_head": text_head,
        })
        if len(hits) >= limit:
            break

    return hits
