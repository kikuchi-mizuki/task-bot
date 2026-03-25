#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
メッセージ解析のテストスクリプト
"""
from ai_service import AIService
import json

def test_message_parsing():
    """ユーザーメッセージの解析をテスト"""
    ai_service = AIService()

    # テストメッセージ
    test_message = "4/8.14.23 6:00~8:30 TSP 移動時間1時間"

    print(f"テストメッセージ: {test_message}")
    print("=" * 50)

    # AIで解析
    result = ai_service.extract_dates_and_times(test_message)

    print("\n解析結果:")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # エラーチェック
    if 'error' in result:
        print("\n⚠️ エラーが発生しました:")
        print(result['error'])
    else:
        print("\n✅ 解析成功")
        print(f"タスクタイプ: {result.get('task_type')}")
        print(f"日付数: {len(result.get('dates', []))}")

        if 'dates' in result:
            for i, date_info in enumerate(result['dates']):
                print(f"\n予定 {i+1}:")
                print(f"  日付: {date_info.get('date')}")
                print(f"  開始: {date_info.get('time')}")
                print(f"  終了: {date_info.get('end_time')}")
                print(f"  タイトル: {date_info.get('title', 'なし')}")

        if 'travel_time_hours' in result:
            print(f"\n移動時間: {result['travel_time_hours']}時間")

if __name__ == "__main__":
    test_message_parsing()
