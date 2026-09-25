"""Create the PostgreSQL schema and import the current data.json once."""

import json
from pathlib import Path

import database


def main():
    if not database.database_enabled():
        raise SystemExit("DATABASE_URL を設定してください")
    database.ensure_schema()
    data_path = Path(__file__).resolve().parent / "data.json"
    if not data_path.exists():
        print("スキーマを作成しました。data.json がないため初期データ移行は省略しました。")
        return
    with data_path.open(encoding="utf-8") as file:
        state = json.load(file)
    if database.import_legacy_state(state):
        print("スキーマを作成し、data.json の初期データを PostgreSQL に移行しました。")
    else:
        print("スキーマは利用可能です。既存データがあるため data.json の再投入は行いませんでした。")


if __name__ == "__main__":
    main()
