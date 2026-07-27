# -*- coding: utf-8 -*-
"""
bridge.py — LM Studioのローカルモデルが「自分専用のメモ」を読み書きするチャットブリッジ

重要: 保存先はデフォルトで ./ai_ssot/ （このスクリプトと同じディレクトリ）。
ユーザー自身の個人SSOT（thoughts_mirror）とは意図的に分離してある。
ローカルLLMの書き込みとユーザー本人の思考ログが混ざると、
どちらのSSOTかわからなくなるため。
--mirror で別パスを指定する場合も、ユーザー本人のthoughts_mirrorではなく
AI専用の別ストアを指すこと。

事前準備:
    1. LM Studioの「Developer」タブでローカルサーバーを起動 (または `lms server start`)
    2. tool calling対応モデルをロードする
       - Qwen2.5-Instruct系 / Llama-3.1-Instruct系など (Native tool use対応モデル推奨)
    3. pip install openai

使い方:
    python bridge.py --model qwen2.5-7b-instruct
    # 保存先を明示したい場合（AI専用の別ストアを指すこと）
    python bridge.py --mirror /path/to/ai_only_store --model qwen2.5-7b-instruct
"""
import argparse
import datetime as dt
import json
import os
import sys
import uuid

from openai import OpenAI

from tools import save_thought, search_thoughts
from tools import _load_schema as _load_thought_schema  # 起動時Fail-Fast用

# 常時想起(Recall)は別レイヤーのエンジンに依存する。本最小版はそれを同梱しないため
# import を任意化し、非同梱時は常時想起を無効化する(save_thought/search_thoughts は動作)。
try:
    from recall import recall_context
except ImportError:
    def recall_context(*_args, **_kwargs):
        return ""

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_AI_SSOT = os.path.join(_THIS_DIR, "ai_ssot")
UTC = dt.timezone.utc


def write_session_file(mirror: str, session_id: str, started_at: str, thought_ids: list) -> None:
    """このセッションで保存したthoughtの一覧を一時ファイルに書く。

    これはjournal_by_dayとは違いSSOTではない派生物（索引は非正＝再生成可能という位置づけ）。
    同一セッションの候補を安価に見つけるための足場に過ぎず、消えても生データ自体は失われない。
    """
    sessions_dir = os.path.join(mirror, "indexes", "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    path = os.path.join(sessions_dir, f"{session_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"session_id": session_id, "started_at": started_at, "thought_ids": thought_ids},
            f,
            ensure_ascii=False,
            indent=2,
        )


def load_genesis_context(mirror: str) -> str:
    """tags:["genesis"]のthoughtをjournal_by_dayから読み、最新のものを返す。

    複数のgenesisレコードが並ぶ状況(訂正はsupersedesで新規追記する原則のため、将来起こりうる)
    に備えて、常に最新のt_utcのものを採用する。厳密なsupersedes関係の解決はまだ実装していない。
    """
    journal_root = os.path.join(mirror, "journal_by_day")
    latest = None
    if not os.path.isdir(journal_root):
        return ""
    for yyyy in sorted(os.listdir(journal_root)):
        yyyy_path = os.path.join(journal_root, yyyy)
        if not os.path.isdir(yyyy_path):
            continue
        for mm in sorted(os.listdir(yyyy_path)):
            mm_path = os.path.join(yyyy_path, mm)
            if not os.path.isdir(mm_path):
                continue
            for fname in sorted(os.listdir(mm_path)):
                if not fname.endswith(".ndjson"):
                    continue
                with open(os.path.join(mm_path, fname), "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        record = entry.get("record", {})
                        if "genesis" in record.get("tags", []):
                            if latest is None or entry["t_utc"] > latest["t_utc"]:
                                latest = entry
    return latest["record"]["text"] if latest else ""


TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "save_thought",
            "description": (
                "あなた（ローカルLLM）自身の作業メモ・気づきを、あなた専用のストアに保存する。"
                "これはユーザー本人のSSOTとは別の、あなた専用の記録領域。"
                "「保存して」「メモして」「記録して」と言われたら呼び出す。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "保存する内容の本文"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "分類用のタグ。最低1個必須、3〜5個程度を推奨。",
                    },
                },
                "required": ["text", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_thoughts",
            "description": (
                "あなた専用ストアに過去保存したthoughtsをキーワードやタグで検索する。"
                "「前に何て言ったっけ」「〜について保存したもの見せて」と言われたら呼び出す。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "検索キーワード（部分一致）"},
                    "tag": {"type": "string", "description": "絞り込みたいタグ"},
                    "limit": {"type": "integer", "description": "取得件数の上限（デフォルト5）"},
                },
                "required": [],
            },
        },
    },
]


def auto_log_turn(mirror: str, text: str, author: str, tags: list,
                   session_id: str, session_started_at: str, session_thought_ids: list) -> None:
    """会話の1ターン(生データ)を無条件でai_ssotに書く（設計原則: 会話中は取捨選択せず全部journalに残す）。

    save_thoughtのtool call有無に関わらず必ず呼ぶ。分類・重要度判定はここでは行わない
    （そうした後処理は別レイヤーのオフラインバッチの仕事）。
    """
    if not text or not text.strip():
        return
    result = save_thought(mirror, text=text, tags=tags, author=author)
    if result.get("status") == "ok":
        session_thought_ids.append(result["hash"])
        write_session_file(mirror, session_id, session_started_at, session_thought_ids)
    else:
        print(f"\n[auto_log warning] 生データの自動保存に失敗: {result}", file=sys.stderr)


def dispatch(name: str, args: dict, mirror: str):
    if name == "save_thought":
        # モデルが自発的に呼ぶ tool call 経路。
        #  ・author="local-llm" を明示的に付与する。
        #  ・"ai_interpretation" をタグに強制付与する(モデルにタグ判断を委ねない)。
        # これはマーカーであって除外ゲートではない。検索・監視で「AIの自発メモ」を区別する用途。
        tags = list(args.get("tags", []))
        if "ai_interpretation" not in tags:
            tags.append("ai_interpretation")
        return save_thought(mirror, text=args.get("text", ""), tags=tags,
                            author="local-llm")
    if name == "search_thoughts":
        return search_thoughts(
            mirror, query=args.get("query", ""), tag=args.get("tag", ""), limit=args.get("limit", 5)
        )
    return {"error": f"unknown tool: {name}"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mirror",
        default=DEFAULT_AI_SSOT,
        help=(
            "AI専用ストアのベースパス（デフォルト: このスクリプト直下のai_ssot/）。"
            "ユーザー本人のthoughts_mirrorを指定しないこと。"
        ),
    )
    parser.add_argument("--base-url", default="http://localhost:1234/v1")
    parser.add_argument("--model", default="google/gemma-4-12b-qat")
    args = parser.parse_args()

    # 起動時Schemaロード(必須)。失敗したら即時エラーで停止する(Fail-Fast)。
    try:
        _load_thought_schema()
    except FileNotFoundError as e:
        print(f"致命的エラー: {e}", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(base_url=args.base_url, api_key="lm-studio")

    session_id = uuid.uuid4().hex[:12]
    session_started_at = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    session_thought_ids: list = []

    system_content = (
        "あなたはローカルで動作するアシスタントです。"
        "あなた自身の気づきを残したいときはsave_thoughtを、"
        "過去の記録を探したいときはsearch_thoughtsを使ってください。"
        "これらはあなた専用のメモであり、ユーザー本人のSSOTとは別物です。"
        "ツールを使わなくていい質問には普通に日本語で答えてください。"
    )
    genesis_text = load_genesis_context(args.mirror)
    if genesis_text:
        system_content += "\n\n【あなたの設計前提(genesis)】\n" + genesis_text

    messages = [{"role": "system", "content": system_content}]

    print("ThoughtsStore x LM Studio bridge 起動（終了は quit）")
    print(f"保存先（AI専用ストア）: {args.mirror}")
    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() == "quit":
            break
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})
        auto_log_turn(
            args.mirror, user_input, "user", ["raw_journal", "user_turn"],
            session_id, session_started_at, session_thought_ids,
        )

        # 常時想起(Recall): 関連する過去の記録を裏で引き、この呼び出しの
        # SYSTEM NOTE としてだけ一時的に添える。messages/journal には残さない(ephemeral)。
        # ※本最小版は recall を同梱しないため、note は常に空(想起は無効)。
        try:
            note = recall_context(args.mirror, user_input,
                                  exclude_ids=set(session_thought_ids))
        except Exception:
            note = ""  # 想起失敗は会話を止めない
        call_messages = messages + [{"role": "system", "content": note}] if note else messages

        response = client.chat.completions.create(
            model=args.model,
            messages=call_messages,
            tools=TOOLS_SCHEMA,
        )
        message = response.choices[0].message

        if message.tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": tc.id, "type": tc.type, "function": tc.function}
                        for tc in message.tool_calls
                    ],
                }
            )

            for tc in message.tool_calls:
                raw_args = tc.function.arguments.strip()
                fn_args = json.loads(raw_args) if raw_args else {}
                result = dispatch(tc.function.name, fn_args, args.mirror)
                print(f"\n[tool call] {tc.function.name}({fn_args}) -> {result}")

                if tc.function.name == "save_thought" and result.get("status") == "ok":
                    session_thought_ids.append(result["hash"])
                    write_session_file(args.mirror, session_id, session_started_at, session_thought_ids)

                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(result, ensure_ascii=False),
                        "tool_call_id": tc.id,
                    }
                )

            final = client.chat.completions.create(model=args.model, messages=messages)
            reply = final.choices[0].message.content
            print(f"\nAssistant: {reply}")
            messages.append({"role": "assistant", "content": reply})
            auto_log_turn(
                args.mirror, reply, "local-llm", ["raw_journal", "assistant_turn"],
                session_id, session_started_at, session_thought_ids,
            )
        else:
            print(f"\nAssistant: {message.content}")
            messages.append({"role": "assistant", "content": message.content})
            auto_log_turn(
                args.mirror, message.content, "local-llm", ["raw_journal", "assistant_turn"],
                session_id, session_started_at, session_thought_ids,
            )


if __name__ == "__main__":
    main()
