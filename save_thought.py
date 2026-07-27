# -*- coding: utf-8 -*-
"""
save_thought.py — Claudeがローカルに直接thoughtを保存するスクリプト

Usage:
    echo '<JSON>' | python save_thought.py --mirror /path/to/thoughts_mirror/

Arguments:
    --mirror    thoughts_mirrorのベースパス（journal_by_day/, indexes/ を含む親フォルダ）
    stdin       保存するthoughtのJSON（1行）

このスクリプトはローカルのファイルにのみ書き込む。
date_local はJST（UTC+9）で決定する。
"""

import json, hashlib, datetime as dt, os, sys, argparse, tempfile
from typing import Optional, Dict

UTC = dt.timezone.utc
JST = dt.timezone(dt.timedelta(hours=9))

import time


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """全書き換えを【原子的】に行う。同ディレクトリの一時ファイルに書き切ってから
    flush→os.fsync→os.replace で本体を差し替える。途中でプロセスが落ちても・エンコード事故が
    起きても、本体(path)は旧内容のまま無傷で残る=truncation(切れ)を構造的に不可能にする。
    os.replace は POSIX/Windows とも原子的置換。newline='\\n' で CRLF 変換を無効化(cp932事故回避)。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)   # 原子的置換
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_append_line(path: str, line: str, encoding: str = "utf-8") -> None:
    """追記型ログ(journal/locator)への1行追記を【耐久化】する。追記自体は原子的置換に
    できない(全読み込みが高コスト)ので、書いた直後に flush→os.fsync で確実にディスクへ落とす。
    末尾に torn line が残らないよう、行末改行込みで1回の write に収める。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding=encoding, newline="\n") as f:
        f.write(line if line.endswith("\n") else line + "\n")
        f.flush()
        os.fsync(f.fileno())


class journal_lock:
    """記録の書き込み経路の排他制御(同一マシン内の競合防止)。依存ゼロ・Windows/POSIX両対応。
    critical section = get_prev_hash → build_record → write_journal → update_indexes を直列化する。
    注: マウント同期をまたぐ【別マシン間】の競合はこれでは防げない(ロックファイルも同期遅延に晒される)。
        そちらは「実書き込みは実機の単一ライタだけ」という運用規律で守る(2026-07-08の事故の教訓)。"""

    def __init__(self, mirror_base, timeout=15.0, stale=60.0, poll=0.05):
        self._path = os.path.join(mirror_base, ".journal.lock")
        self._timeout = timeout
        self._stale = stale
        self._poll = poll
        self._fd = None

    def __enter__(self):
        deadline = time.time() + self._timeout
        while True:
            try:
                self._fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self._path) > self._stale:
                        os.unlink(self._path)  # 異常終了で残った古いロックは奪う
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    raise TimeoutError("journal lock timeout: " + self._path)
                time.sleep(self._poll)

    def __exit__(self, *a):
        # 解放はベストエフォート。書き込みは既に完了しているので、close/unlink の失敗
        # (権限・マウント同期等)で成功した save をクラッシュさせない。取り残したロックは
        # 次の書き手の stale-timeout(既定60s)が回収する。
        try:
            if self._fd is not None:
                os.close(self._fd)
        except Exception:
            pass
        try:
            os.unlink(self._path)
        except Exception:
            pass


def utc_now_iso() -> str:
    return dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def to_jst(t_utc_str: str) -> dt.datetime:
    """UTC ISO文字列をJSTのdatetimeに変換する"""
    return dt.datetime.fromisoformat(t_utc_str.replace("Z", "+00:00")).astimezone(JST)


def parse_last(ndjson: str) -> Optional[Dict]:
    if not ndjson.strip():
        return None
    try:
        return json.loads(ndjson.strip().splitlines()[-1])
    except Exception:
        return None


def build_record(author: str, prev_hash: Optional[str], record: Dict) -> Dict:
    t_utc = utc_now_iso()
    payload = json.dumps(
        {"t_utc": t_utc, "prev_hash": prev_hash, "record": record},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    h = sha256_hex(payload)
    return {
        "id": h[:12], "t_utc": t_utc, "author": author,
        "prev_hash": prev_hash, "hash": h,
        "algo": "sha256({t_utc,prev_hash,record})", "v": 1, "record": record,
    }


def jst_date_parts(t_utc_str: str) -> tuple[str, str, str]:
    """t_utc文字列からJSTのyyyy, mm, ymdを返す"""
    jst = to_jst(t_utc_str)
    yyyy = f"{jst.year:04d}"
    mm = f"{jst.month:02d}"
    ymd = f"{jst.year:04d}-{jst.month:02d}-{jst.day:02d}"
    return yyyy, mm, ymd


def write_journal(mirror_base: str, record_obj: Dict) -> tuple[str, int]:
    """journal_by_day に追記し (filename, line_num) を返す"""
    yyyy, mm, ymd = jst_date_parts(record_obj["t_utc"])
    filename = f"{ymd}.ndjson"

    local_dir = os.path.join(mirror_base, "journal_by_day", yyyy, mm)
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, filename)

    existing = ""
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            existing = f.read()

    line_num = len([l for l in existing.splitlines() if l.strip()]) + 1

    # 追記+fsync。SSOT正本なので torn line を残さないよう耐久化する。
    atomic_append_line(local_path, json.dumps(record_obj, ensure_ascii=False))

    return filename, line_num


def get_prev_hash(mirror_base: str) -> Optional[str]:
    """今日（JST）のファイルの最終ハッシュを返す"""
    now_jst = dt.datetime.now(JST)
    yyyy = f"{now_jst.year:04d}"
    mm = f"{now_jst.month:02d}"
    ymd = f"{now_jst.year:04d}-{now_jst.month:02d}-{now_jst.day:02d}"
    path = os.path.join(mirror_base, "journal_by_day", yyyy, mm, f"{ymd}.ndjson")

    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    last = parse_last(content)
    return last.get("hash") if last else None


def update_indexes(mirror_base: str, record_obj: Dict, filename: str, line_num: int) -> None:
    """thought_map.json と locator.jsonl をローカルで更新する"""
    indexes_dir = os.path.join(mirror_base, "indexes")
    os.makedirs(indexes_dir, exist_ok=True)

    inner = record_obj.get("record", {})
    tags = inner.get("tags", [])
    title = inner.get("title", "")
    text = inner.get("text", "")
    text_head = (title or text)[:80]
    # date_local もJSTで決定
    _, _, date_local = jst_date_parts(record_obj["t_utc"])
    full_hash = record_obj["hash"]

    # --- thought_map.json ---
    thought_map_path = os.path.join(indexes_dir, "thought_map.json")
    try:
        if os.path.exists(thought_map_path):
            with open(thought_map_path, "r", encoding="utf-8") as f:
                tm = json.load(f)
        else:
            tm = {"meta": {"version": "local", "build_utc": record_obj["t_utc"],
                           "record_count": 0, "input_files": []}, "thoughts": []}

        tm["thoughts"].append({
            "id": full_hash,
            "type": "thought",
            "t_utc": record_obj["t_utc"],
            "date_local": date_local,
            "tags": tags,
            "text_head": text_head,
            "source": {"file": filename, "line": line_num},
            "source_is_build_time": False,
            "inferred_context_thread": None,
            "inferred_context_event_id": None,
            "inference_rule": None,
        })
        tm["meta"]["record_count"] = len(tm["thoughts"])
        if filename not in tm["meta"].get("input_files", []):
            tm["meta"].setdefault("input_files", []).append(filename)

        # 全書き換え=原子的に。open("w")直後の切り詰めで空/半端に壊れる事故を潰す。
        atomic_write_text(thought_map_path,
                          json.dumps(tm, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"⚠️ thought_map.json 更新失敗: {e}", file=sys.stderr)

    # --- locator.jsonl ---
    locator_path = os.path.join(indexes_dir, "locator.jsonl")
    try:
        atomic_append_line(locator_path, json.dumps({
            "id": full_hash,
            "type": "thought",
            "t_utc": record_obj["t_utc"],
            "date_local": date_local,
            "tags": tags,
            "text_head": text_head,
            "source": {"file": filename, "line": line_num},
            "source_is_build_time": False,
        }, ensure_ascii=False))
    except Exception as e:
        print(f"⚠️ locator.jsonl 更新失敗: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mirror", required=True, help="thoughts_mirror フォルダパス")
    args = parser.parse_args()

    raw = sys.stdin.read().strip()
    if not raw:
        print("❌ stdin が空です", file=sys.stderr)
        sys.exit(1)

    payload = json.loads(raw)
    author = payload.get("author", "unknown")
    record = payload.get("record", {})
    if not record:
        print("❌ record が空です", file=sys.stderr)
        sys.exit(1)

    with journal_lock(args.mirror):
        prev_hash = get_prev_hash(args.mirror)
        record_obj = build_record(author, prev_hash, record)
        filename, line_num = write_journal(args.mirror, record_obj)
        update_indexes(args.mirror, record_obj, filename, line_num)

    print(json.dumps({
        "status": "ok",
        "file": filename,
        "line": line_num,
        "hash": record_obj["hash"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
