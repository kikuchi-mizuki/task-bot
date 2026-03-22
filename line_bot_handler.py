from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime, timedelta
from dateutil import parser
import pytz
import re
from calendar_service import GoogleCalendarService
from ai_service import AIService
from config import Config
from db import DBHelper
import logging

logger = logging.getLogger("line_bot_handler")

class LineBotHandler:
    def __init__(self):
        # LINE Bot API クライアント初期化（標準）
        if not Config.LINE_CHANNEL_ACCESS_TOKEN:
            raise ValueError("LINE_CHANNEL_ACCESS_TOKEN environment variable is not set")
        if not Config.LINE_CHANNEL_SECRET:
            raise ValueError("LINE_CHANNEL_SECRET environment variable is not set")
            
        self.line_bot_api = LineBotApi(Config.LINE_CHANNEL_ACCESS_TOKEN)
        self.handler = WebhookHandler(Config.LINE_CHANNEL_SECRET)
        
        # カスタムセッション設定をグローバルに適用
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        # リトライ戦略を設定（より詳細な設定）
        retry_strategy = Retry(
            total=5,  # 最大リトライ回数を増加
            backoff_factor=2,  # バックオフ係数を増加
            status_forcelist=[429, 500, 502, 503, 504, 520, 521, 522, 523, 524],  # リトライするHTTPステータスコードを拡張
            allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"],  # 全HTTPメソッドでリトライ
            raise_on_status=False,  # ステータスエラーで例外を発生させない
        )
        
        # アダプターを設定
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
        
        # グローバルセッション設定
        session = requests.Session()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.timeout = (15, 45)  # (接続タイムアウト, 読み取りタイムアウト) を増加
        
        # LINE Bot SDKの内部セッションを置き換え
        self.line_bot_api._session = session
        
        # DBヘルパーの初期化
        self.db_helper = DBHelper()
        
        try:
            self.calendar_service = GoogleCalendarService()
        except Exception as e:
            print(f"Google Calendarサービス初期化エラー: {e}")
            self.calendar_service = None
            
        try:
            self.ai_service = AIService()
        except Exception as e:
            print(f"AIサービス初期化エラー: {e}")
            self.ai_service = None
            
        self.jst = pytz.timezone('Asia/Tokyo')
    
    def _check_user_auth(self, line_user_id):
        """ユーザーの認証状態をチェック"""
        return self.db_helper.user_exists(line_user_id)
    
    def _send_auth_guide(self, line_user_id):
        """認証案内メッセージを送信"""
        # ワンタイムコードを生成
        code = self.db_helper.generate_onetime_code(line_user_id)
        
        # 認証URLを生成（環境変数から取得）
        import os
        base_url = os.getenv('BASE_URL', 'https://web-production-xxxx.up.railway.app')
        auth_url = f"{base_url}/onetime_login"
        
        message = f"""Google Calendar認証が必要です。

🔐 ワンタイムコード: {code}

📱 認証手順:
1. 下のURLをクリックまたはコピー
2. ワンタイムコードを入力
3. Googleアカウントで認証

🔗 認証URL:
{auth_url}

⚠️ コードの有効期限は10分です
"""
        return TextSendMessage(text=message)
    
    def handle_message(self, event):
        """メッセージを処理します"""
        user_message = event.message.text
        line_user_id = event.source.user_id

        # Google認証未完了なら必ず認証案内を返す
        if not self._check_user_auth(line_user_id):
            return self._send_auth_guide(line_user_id)

        # 「はい」返答による強制追加判定
        if user_message.strip() in ["はい", "追加", "OK", "Yes", "yes"]:
            pending_json = self.db_helper.get_pending_event(line_user_id)
            if pending_json:
                import json
                events_data = json.loads(pending_json)
                
                # 単一イベントか複数イベントかを判定
                if isinstance(events_data, list):
                    # 複数イベント（移動時間含む）の場合
                    total_pending_events = len(events_data)

                    # 20件以上の場合はバックグラウンド処理とBatch APIを使用
                    if total_pending_events >= 20:
                        print(f"[DEBUG] 大量の保留イベント検出: {total_pending_events}件、バックグラウンド処理を使用")

                        # pending_eventsを削除
                        self.db_helper.delete_pending_event(line_user_id)

                        # バックグラウンド処理
                        import threading

                        def background_add_pending_events():
                            """バックグラウンドで保留中の予定を追加"""
                            try:
                                from dateutil import parser

                                # Batch API用にイベントデータを準備
                                batch_events = []
                                for event_info in events_data:
                                    try:
                                        start_datetime = parser.parse(event_info['start_datetime'])
                                        end_datetime = parser.parse(event_info['end_datetime'])

                                        if start_datetime.tzinfo is None:
                                            start_datetime = self.jst.localize(start_datetime)
                                        if end_datetime.tzinfo is None:
                                            end_datetime = self.jst.localize(end_datetime)

                                        batch_events.append({
                                            'title': event_info['title'],
                                            'start_datetime': start_datetime,
                                            'end_datetime': end_datetime,
                                            'description': event_info.get('description', ''),
                                            'original_info': event_info
                                        })
                                    except Exception as e:
                                        print(f"[DEBUG] イベントデータ準備エラー: {e}")

                                # Batch APIで追加
                                success_count, failed_count, results = self.calendar_service.add_events_batch(
                                    batch_events,
                                    line_user_id=line_user_id
                                )

                                # 結果メッセージを構築
                                if success_count > 0:
                                    result_text = f"✅ 処理が完了しました！\n\n{success_count}件の予定を追加しました"
                                    if failed_count > 0:
                                        result_text += f"\n\n⚠️ {failed_count}件の予定を追加できませんでした"
                                else:
                                    result_text = "❌ 予定を追加できませんでした"

                                # プッシュメッセージで結果を送信
                                self.line_bot_api.push_message(
                                    line_user_id,
                                    TextSendMessage(text=result_text)
                                )
                                print(f"[DEBUG] 保留イベント処理完了: 成功={success_count}件, 失敗={failed_count}件")

                            except Exception as e:
                                print(f"[DEBUG] バックグラウンド処理エラー: {e}")
                                import traceback
                                traceback.print_exc()
                                try:
                                    self.line_bot_api.push_message(
                                        line_user_id,
                                        TextSendMessage(text=f"❌ 予定追加中にエラーが発生しました: {str(e)}")
                                    )
                                except Exception as push_error:
                                    print(f"[DEBUG] プッシュメッセージ送信エラー: {push_error}")

                        # バックグラウンドスレッドを起動
                        thread = threading.Thread(target=background_add_pending_events)
                        thread.daemon = True
                        thread.start()

                        # 処理中メッセージを即座に返す
                        return TextSendMessage(text=f"⏳ {total_pending_events}件の予定を追加中です...\n処理完了までお待ちください。")

                    # 20件未満の場合は従来の処理（1件ずつ追加）
                    added_events = []
                    failed_events = []

                    for event_info in events_data:
                        try:
                            from dateutil import parser
                            start_datetime = parser.parse(event_info['start_datetime'])
                            end_datetime = parser.parse(event_info['end_datetime'])

                            # 既にタイムゾーンが設定されている場合はそのまま使用、そうでなければJSTを設定
                            if start_datetime.tzinfo is None:
                                start_datetime = self.jst.localize(start_datetime)
                            if end_datetime.tzinfo is None:
                                end_datetime = self.jst.localize(end_datetime)

                            if not self.calendar_service:
                                failed_events.append({
                                    'title': event_info['title'],
                                    'reason': 'カレンダーサービスが初期化されていません'
                                })
                                continue

                            success, message, result = self.calendar_service.add_event(
                                event_info['title'],
                                start_datetime,
                                end_datetime,
                                event_info.get('description', ''),
                                line_user_id=line_user_id,
                                force_add=True
                            )

                            if success:
                                # 日時をフォーマット
                                from datetime import datetime
                                import pytz
                                jst = pytz.timezone('Asia/Tokyo')
                                start_dt = start_datetime.astimezone(jst)
                                end_dt = end_datetime.astimezone(jst)
                                weekday = "月火水木金土日"[start_dt.weekday()]
                                date_str = f"{start_dt.month}/{start_dt.day}（{weekday}）"
                                time_str = f"{start_dt.strftime('%H:%M')}〜{end_dt.strftime('%H:%M')}"

                                added_events.append({
                                    'title': event_info['title'],
                                    'time': f"{date_str}{time_str}"
                                })
                            else:
                                failed_events.append({
                                    'title': event_info['title'],
                                    'reason': message
                                })
                        except Exception as e:
                            failed_events.append({
                                'title': event_info.get('title', '予定'),
                                'reason': str(e)
                            })

                    self.db_helper.delete_pending_event(line_user_id)
                    
                    # 結果メッセージを構築（移動時間を含む場合は統一形式）
                    if added_events:
                        # 移動時間が含まれているかチェック
                        has_travel = any('移動時間' in event['title'] for event in added_events)
                        
                        if has_travel and len(added_events) > 1:
                            # 移動時間を含む場合は統一形式で表示
                            response_text = "✅予定を追加しました！\n\n"
                            
                            # 日付を取得（最初の予定から）
                            first_event = added_events[0]
                            time_str = first_event['time']
                            # "10/18 (土)19:00〜20:00" から "10/18 (土)" を抽出
                            import re
                            date_match = re.search(r'(\d{1,2}/\d{1,2}\s*\([月火水木金土日]\)\s*)', time_str)
                            date_part = date_match.group(1).strip() if date_match else time_str
                            response_text += f"{date_part}\n"
                            response_text += "────────\n"
                            
                            # 時間順でソート（開始時間でソート）
                            def get_start_time(event):
                                time_str = event['time']
                                # "10/18 (土)19:00〜20:00" から "19:00〜20:00" を抽出
                                time_match = re.search(r'(\d{1,2}:\d{2}〜\d{1,2}:\d{2})', time_str)
                                time_part = time_match.group(1) if time_match else time_str
                                start_time = time_part.split('〜')[0]  # "19:00〜20:00" -> "19:00"
                                return start_time
                            
                            sorted_events = sorted(added_events, key=get_start_time)
                            
                            # 各予定を番号付きで表示
                            for i, event in enumerate(sorted_events, 1):
                                # 時間部分を抽出（"10:00~11:00" の形式）
                                time_str = event['time']
                                time_match = re.search(r'(\d{1,2}:\d{2}〜\d{1,2}:\d{2})', time_str)
                                time_part = time_match.group(1) if time_match else time_str
                                response_text += f"{i}. {event['title']}\n"
                                response_text += f"🕐 {time_part}\n"
                            
                            response_text += "────────"
                        else:
                            # 通常の表示形式
                            response_text = "✅予定を追加しました！\n\n"
                            for event in added_events:
                                response_text += f"📅{event['title']}\n{event['time']}\n"
                        
                        if failed_events:
                            response_text += "\n\n⚠️追加できなかった予定:\n"
                            for event in failed_events:
                                response_text += f"• {event['title']} - {event['reason']}\n"
                    else:
                        response_text = "❌予定を追加できませんでした。\n\n"
                        for event in failed_events:
                            response_text += f"• {event['title']} - {event['reason']}\n"
                    
                    return TextSendMessage(text=response_text)
                else:
                    # 単一イベントの場合（従来の処理）
                    event_info = events_data
                    from dateutil import parser
                    start_datetime = parser.parse(event_info['start_datetime'])
                    end_datetime = parser.parse(event_info['end_datetime'])
                    # 既にタイムゾーンが設定されている場合はそのまま使用、そうでなければJSTを設定
                    if start_datetime.tzinfo is None:
                        start_datetime = self.jst.localize(start_datetime)
                    if end_datetime.tzinfo is None:
                        end_datetime = self.jst.localize(end_datetime)
                    if not self.calendar_service or not self.ai_service:
                        return TextSendMessage(text="カレンダーサービスまたはAIサービスが初期化されていません。")
                    success, message, result = self.calendar_service.add_event(
                        event_info['title'],
                        start_datetime,
                        end_datetime,
                        event_info.get('description', ''),
                        line_user_id=line_user_id,
                        force_add=True
                    )
                    self.db_helper.delete_pending_event(line_user_id)
                    response_text = self.ai_service.format_event_confirmation(success, message, result)
                    return TextSendMessage(text=response_text)
        else:
            # 「はい」以外の返答でpending_eventsがあれば削除し、キャンセルメッセージを返す
            pending_json = self.db_helper.get_pending_event(line_user_id)
            if pending_json:
                self.db_helper.delete_pending_event(line_user_id)
                return TextSendMessage(text="予定追加をキャンセルしました。")
        
        try:
            # 環境変数が設定されていない場合の処理
            if not Config.LINE_CHANNEL_ACCESS_TOKEN or not Config.LINE_CHANNEL_SECRET:
                return TextSendMessage(text="LINE Botの設定が完了していません。環境変数を設定してください。")
            
            if not self.ai_service:
                return TextSendMessage(text="AIサービスの初期化に失敗しました。OpenAI APIキーを設定してください。")

            # 会話履歴を取得
            conversation_history = self.db_helper.get_conversation_history(line_user_id, limit=5)

            # ユーザーメッセージを会話履歴に保存
            self.db_helper.save_conversation_message(line_user_id, 'user', user_message)

            # AIを使ってメッセージの意図を判断（会話履歴を渡す）
            ai_result = self.ai_service.extract_dates_and_times(user_message, conversation_history)
            print(f"[DEBUG] ai_result: {ai_result}")
            
            if 'error' in ai_result:
                # AI処理に失敗した場合、ガイダンスメッセージを返す
                return TextSendMessage(text="日時の送信で空き時間が分かります！\n日時と内容の送信で予定を追加します！\n\n例：\n・「明日の空き時間」\n・「7/15 15:00〜16:00の空き時間」\n・「明日の午前9時から会議を追加して」\n・「来週月曜日の14時から打ち合わせ」")
            
            # タスクタイプに基づいて処理
            task_type = ai_result.get('task_type', 'add_event')

            # 応答を生成し、会話履歴に保存
            response_message = None

            if task_type == 'show_schedule':
                print(f"[DEBUG] show_schedule: {ai_result.get('dates', [])}")
                response_message = self._handle_show_schedule(ai_result.get('dates', []), line_user_id)
            elif task_type == 'availability_check':
                print(f"[DEBUG] dates_info: {ai_result.get('dates', [])}")
                response_message = self._handle_availability_check(ai_result.get('dates', []), line_user_id)
            elif task_type == 'add_event':
                # 予定追加時の重複確認ロジック（複数予定対応）
                if not self.calendar_service:
                    return TextSendMessage(text="カレンダーサービスが初期化されていません。")

                dates = ai_result.get('dates', [])
                travel_time_hours = ai_result.get('travel_time_hours')
                if not dates:
                    return TextSendMessage(text="イベント情報を正しく認識できませんでした。\n\n例: 「明日の午前9時から会議を追加して」\n「来週月曜日の14時から打ち合わせ」")

                # 複数の予定を処理
                response_message = self._handle_multiple_events(dates, line_user_id, travel_time_hours)
            else:
                # 未対応コマンドの場合もガイダンスメッセージ
                response_message = TextSendMessage(text="日時の送信で空き時間が分かります！\n日時と内容の送信で予定を追加します！\n\n例：\n・「明日の空き時間」\n・「7/15 15:00〜16:00の空き時間」\n・「明日の午前9時から会議を追加して」\n・「来週月曜日の14時から打ち合わせ」")

            # 応答を会話履歴に保存
            if response_message and hasattr(response_message, 'text'):
                self.db_helper.save_conversation_message(line_user_id, 'assistant', response_message.text)
                # 古い会話履歴をクリーンアップ（最新20件のみ保持）
                self.db_helper.clear_old_conversation_history(line_user_id, keep_count=20)

            return response_message

        except Exception as e:
            return TextSendMessage(text=f"エラーが発生しました: {str(e)}")
    
    def _handle_multiple_events(self, dates, line_user_id, travel_time_hours=None):
        """複数の予定を処理します"""
        try:
            from dateutil import parser
            import json

            added_events = []
            failed_events = []

            # 移動時間を含む予定を展開
            expanded_dates = []
            global_travel_time_hours = travel_time_hours

            for i, date_info in enumerate(dates):
                print(f"[DEBUG] _handle_multiple_events: date_info[{i}]のタイプ={type(date_info)}, 値={date_info}")

                # 文字列や辞書以外の場合はスキップ
                if not isinstance(date_info, dict):
                    print(f"[WARNING] date_info[{i}]が辞書でないためスキップ: タイプ={type(date_info)}")
                    continue
                # 個別の移動時間が指定されている場合はそれを優先、なければ全体の設定を使用
                item_travel_time_hours = date_info.get('travel_time_hours', global_travel_time_hours)
                if item_travel_time_hours:
                    # メイン予定の日時を取得
                    date_str = date_info.get('date')
                    time_str = date_info.get('time')
                    end_time_str = date_info.get('end_time')
                    title = date_info.get('title', '予定')

                    if date_str and time_str and end_time_str:
                        from datetime import datetime, timedelta

                        # メイン予定の開始・終了時刻を計算
                        main_start = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                        main_end = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M")

                        # 移動時間（時間単位を分単位に変換）
                        travel_minutes = int(item_travel_time_hours * 60)
                        travel_delta = timedelta(minutes=travel_minutes)

                        # 前の移動時間予定
                        before_travel_start = main_start - travel_delta
                        before_travel_end = main_start
                        expanded_dates.append({
                            'date': before_travel_start.strftime('%Y-%m-%d'),
                            'time': before_travel_start.strftime('%H:%M'),
                            'end_time': before_travel_end.strftime('%H:%M'),
                            'title': '移動時間（行き）',
                            'description': f'{title}への移動'
                        })

                        # メイン予定
                        expanded_dates.append({
                            'date': date_str,
                            'time': time_str,
                            'end_time': end_time_str,
                            'title': title,
                            'description': date_info.get('description', '')
                        })

                        # 後の移動時間予定
                        after_travel_start = main_end
                        after_travel_end = main_end + travel_delta
                        expanded_dates.append({
                            'date': after_travel_start.strftime('%Y-%m-%d'),
                            'time': after_travel_start.strftime('%H:%M'),
                            'end_time': after_travel_end.strftime('%H:%M'),
                            'title': '移動時間（帰り）',
                            'description': f'{title}からの移動'
                        })
                    else:
                        # 日時が不完全な場合はそのまま追加
                        expanded_dates.append(date_info)
                else:
                    # 移動時間がない場合はそのまま追加
                    expanded_dates.append(date_info)

            # 展開後の予定リストを使用
            dates = expanded_dates if expanded_dates else dates

            # 効率化: 日付ごとにグループ化して重複チェック
            from collections import defaultdict
            from datetime import datetime, timedelta

            events_by_date = defaultdict(list)
            for date_info in dates:
                # 辞書でない場合はスキップ
                if not isinstance(date_info, dict):
                    print(f"[WARNING] events_by_date処理: date_infoが辞書でないためスキップ: {type(date_info)}")
                    continue

                date_str = date_info.get('date')
                if date_str:
                    events_by_date[date_str].append(date_info)

            # 日付ごとにカレンダーイベントを取得（1日1回のAPIコール）
            conflicting_dates = {}  # 日付ごとの重複情報
            non_conflicting_events = []  # 重複のないイベント
            existing_events_cache = {}

            for date_str, date_events in events_by_date.items():
                try:
                    # その日の最小開始時刻と最大終了時刻を計算
                    min_time = "23:59"
                    max_time = "00:00"

                    for event_info in date_events:
                        # 辞書でない場合はスキップ
                        if not isinstance(event_info, dict):
                            print(f"[WARNING] event_infoが辞書でないためスキップ: {type(event_info)}")
                            continue

                        time_str = event_info.get('time', '00:00')
                        end_time_str = event_info.get('end_time')

                        # 終了時間が未設定の場合は1時間後
                        if not end_time_str or end_time_str == time_str:
                            time_obj = datetime.strptime(time_str, "%H:%M")
                            end_time_obj = time_obj + timedelta(hours=1)
                            end_time_str = end_time_obj.strftime("%H:%M")
                            event_info['end_time'] = end_time_str

                        if time_str < min_time:
                            min_time = time_str
                        if end_time_str > max_time:
                            max_time = end_time_str

                    # その日の範囲で既存予定を1回取得
                    start_datetime_str = f"{date_str}T{min_time}:00+09:00"
                    end_datetime_str = f"{date_str}T{max_time}:00+09:00"

                    start_datetime = parser.parse(start_datetime_str)
                    end_datetime = parser.parse(end_datetime_str)

                    if start_datetime.tzinfo is None:
                        start_datetime = self.jst.localize(start_datetime)
                    if end_datetime.tzinfo is None:
                        end_datetime = self.jst.localize(end_datetime)

                    # その日の既存予定を取得（キャッシュ）
                    existing_events = self.calendar_service.get_events_for_time_range(start_datetime, end_datetime, line_user_id)
                    existing_events_cache[date_str] = existing_events

                    # 各予定に対して重複チェック（メモリ内で実施）
                    date_has_conflict = False
                    for event_info in date_events:
                        time_str = event_info.get('time')
                        end_time_str = event_info.get('end_time')
                        title = event_info.get('title', '予定')

                        if not time_str or not end_time_str:
                            continue

                        # 時刻をパース
                        event_start = parser.parse(f"{date_str}T{time_str}:00+09:00")
                        event_end = parser.parse(f"{date_str}T{end_time_str}:00+09:00")

                        if event_start.tzinfo is None:
                            event_start = self.jst.localize(event_start)
                        if event_end.tzinfo is None:
                            event_end = self.jst.localize(event_end)

                        # 既存予定との重複をチェック
                        has_conflict = False
                        for existing in existing_events:
                            existing_start = parser.parse(existing.get('start', ''))
                            existing_end = parser.parse(existing.get('end', ''))

                            # 重複判定：新予定の開始が既存の終了より前 AND 新予定の終了が既存の開始より後
                            if event_start < existing_end and event_end > existing_start:
                                has_conflict = True
                                date_has_conflict = True

                                # 日付ごとに重複情報を保存
                                if date_str not in conflicting_dates:
                                    conflicting_dates[date_str] = {
                                        'events': [],
                                        'conflicts': []
                                    }

                                # 既存の重複リストに同じものがなければ追加
                                conflict_exists = any(
                                    c['title'] == existing.get('title', '予定なし') and
                                    c['start'] == existing.get('start', '') and
                                    c['end'] == existing.get('end', '')
                                    for c in conflicting_dates[date_str]['conflicts']
                                )
                                if not conflict_exists:
                                    conflicting_dates[date_str]['conflicts'].append({
                                        'title': existing.get('title', '予定なし'),
                                        'start': existing.get('start', ''),
                                        'end': existing.get('end', '')
                                    })

                    # この日に重複がない場合は、自動追加リストに追加
                    # 重複がある場合は、その日のすべてのイベントを記録
                    if not date_has_conflict:
                        non_conflicting_events.extend(date_events)
                    else:
                        # この日に重複がある場合は、その日のすべてのイベントを記録
                        # （移動時間付きの予定の場合、行き・メイン・帰りの3つをまとめて記録）
                        if date_str in conflicting_dates:
                            conflicting_dates[date_str]['events'] = date_events

                except Exception as e:
                    print(f"[DEBUG] 日付 {date_str} の重複チェック中にエラー: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

            # 重複していない予定を自動的に追加（Batch API使用）
            auto_added_count = 0
            auto_added_dates = []
            batch_events = []  # スコープを広げるため、ここで定義

            # 大量の予定（20件以上）の場合、バックグラウンド処理を使用
            use_background = False
            if non_conflicting_events:
                total_events = len(non_conflicting_events)
                print(f"[DEBUG] 重複のない予定をBatch APIで一括追加: {total_events}件")

                # 20件以上の場合はバックグラウンド処理
                if total_events >= 20:
                    use_background = True
                    print(f"[DEBUG] 大量予定検出: バックグラウンド処理を使用")

                # Batch API用にイベントデータを準備
                for event_info in non_conflicting_events:
                    try:
                        date_str = event_info.get('date')
                        time_str = event_info.get('time')
                        end_time_str = event_info.get('end_time')
                        title = event_info.get('title', '予定')
                        description = event_info.get('description', '')

                        if not date_str or not time_str or not end_time_str:
                            continue

                        # 日時文字列を構築
                        start_datetime_str = f"{date_str}T{time_str}:00+09:00"
                        end_datetime_str = f"{date_str}T{end_time_str}:00+09:00"

                        # 日時をパース
                        start_datetime = parser.parse(start_datetime_str)
                        end_datetime = parser.parse(end_datetime_str)

                        if start_datetime.tzinfo is None:
                            start_datetime = self.jst.localize(start_datetime)
                        if end_datetime.tzinfo is None:
                            end_datetime = self.jst.localize(end_datetime)

                        batch_events.append({
                            'title': title,
                            'start_datetime': start_datetime,
                            'end_datetime': end_datetime,
                            'description': description,
                            'date_str': date_str  # 日付を記録
                        })

                    except Exception as e:
                        print(f"[DEBUG] イベントデータ準備エラー: {e}")
                        continue

                # Batch APIで一括追加
                if batch_events:
                    if use_background:
                        # バックグラウンド処理を開始
                        import threading

                        def background_add_events():
                            """バックグラウンドで予定を追加し、完了後にプッシュメッセージを送信"""
                            try:
                                print(f"[DEBUG] バックグラウンド処理開始: {len(batch_events)}件")
                                success_count, failed_count, results = self.calendar_service.add_events_batch(
                                    batch_events,
                                    line_user_id=line_user_id
                                )
                                print(f"[DEBUG] バックグラウンド処理完了: 成功={success_count}件, 失敗={failed_count}件")

                                # 追加された日付を集計
                                added_dates = []
                                for event_data in batch_events[:success_count]:
                                    date_str = event_data['date_str']
                                    if date_str not in added_dates:
                                        added_dates.append(date_str)

                                # 結果メッセージを構築
                                if success_count > 0:
                                    formatted_dates = []
                                    for date_str in sorted(added_dates):
                                        dt = parser.parse(date_str)
                                        formatted_dates.append(f"{dt.month}/{dt.day}")

                                    result_text = f"✅ 処理が完了しました！\n\n{len(added_dates)}日分の予定を追加しました\n（{', '.join(formatted_dates)}）"

                                    if failed_count > 0:
                                        result_text += f"\n\n⚠️ {failed_count}件の予定を追加できませんでした"
                                else:
                                    result_text = "❌ 予定を追加できませんでした"

                                # プッシュメッセージで結果を送信
                                self.line_bot_api.push_message(
                                    line_user_id,
                                    TextSendMessage(text=result_text)
                                )
                                print(f"[DEBUG] 結果メッセージ送信完了")

                            except Exception as e:
                                print(f"[DEBUG] バックグラウンド処理エラー: {e}")
                                import traceback
                                traceback.print_exc()
                                # エラー時もプッシュメッセージで通知
                                try:
                                    self.line_bot_api.push_message(
                                        line_user_id,
                                        TextSendMessage(text=f"❌ 予定追加中にエラーが発生しました: {str(e)}")
                                    )
                                except Exception as push_error:
                                    print(f"[DEBUG] プッシュメッセージ送信エラー: {push_error}")

                        # バックグラウンドスレッドを起動
                        thread = threading.Thread(target=background_add_events)
                        thread.daemon = True
                        thread.start()

                        # 処理中メッセージを即座に返す
                        processing_message = f"⏳ {total_events}件の予定を追加中です...\n処理完了までお待ちください。"
                        if conflicting_dates:
                            # 重複がある場合は、処理中メッセージの後に重複確認を表示
                            auto_added_count = -1  # バックグラウンド処理中を示すフラグ
                        else:
                            # 重複がない場合は、処理中メッセージのみを返して終了
                            return TextSendMessage(text=processing_message)
                    else:
                        # 通常処理（20件未満）
                        success_count, failed_count, results = self.calendar_service.add_events_batch(
                            batch_events,
                            line_user_id=line_user_id
                        )

                        auto_added_count = success_count

                        # 追加された日付を集計
                        for event_data in batch_events[:success_count]:
                            date_str = event_data['date_str']
                            if date_str not in auto_added_dates:
                                auto_added_dates.append(date_str)

                        print(f"[DEBUG] Batch API結果: 成功={success_count}件, 失敗={failed_count}件")

            # 重複が見つかった場合は日付ごとに確認メッセージを表示
            if conflicting_dates:
                print(f"[DEBUG] 重複予定を検出（{len(conflicting_dates)}日分）")
                response_text = ""

                # バックグラウンド処理中の場合
                if auto_added_count == -1:
                    response_text += f"⏳ {len(non_conflicting_events)}件の予定を追加中です...\n処理完了までお待ちください。\n\n"
                # 自動追加された予定がある場合（通常処理）
                elif auto_added_count > 0:
                    # 日付を整形
                    formatted_dates = []
                    for date_str in sorted(auto_added_dates):
                        dt = parser.parse(date_str)
                        formatted_dates.append(f"{dt.month}/{dt.day}")

                    response_text += f"✅ {len(auto_added_dates)}日分の予定を追加しました\n"
                    response_text += f"（{', '.join(formatted_dates)}）\n\n"

                response_text += "⚠️ 以下の日付で既存予定と重複しています:\n\n"

                for date_str in sorted(conflicting_dates.keys()):
                    dt = parser.parse(date_str)
                    response_text += f"【{dt.month}/{dt.day}（{['月','火','水','木','金','土','日'][dt.weekday()]}）】\n"

                    for conflict in conflicting_dates[date_str]['conflicts']:
                        # 時間をフォーマット
                        start_time = conflict['start']
                        end_time = conflict['end']
                        if 'T' in start_time:
                            start_dt = parser.parse(start_time)
                            end_dt = parser.parse(end_time)
                            start_dt = start_dt.astimezone(self.jst)
                            end_dt = end_dt.astimezone(self.jst)
                            time_str = f"{start_dt.strftime('%H:%M')}~{end_dt.strftime('%H:%M')}"
                        else:
                            time_str = f"{start_time}~{end_time}"

                        response_text += f"  - {conflict['title']} ({time_str})\n"

                response_text += "\nこれらの日も追加しますか？\n「はい」と返信してください。"

                # 重複している日のイベントのみをpending_eventsに保存
                pending_events = []
                for date_str, conflict_data in conflicting_dates.items():
                    for event_info in conflict_data['events']:
                        event_date_str = event_info.get('date')
                        event_time_str = event_info.get('time')
                        event_end_time_str = event_info.get('end_time')
                        event_title = event_info.get('title', '予定')
                        event_description = event_info.get('description', '')

                        if not event_date_str or not event_time_str:
                            continue

                        # 終了時間が設定されていない場合は1時間後に設定
                        if not event_end_time_str or event_end_time_str == event_time_str:
                            time_obj = datetime.strptime(event_time_str, "%H:%M")
                            end_time_obj = time_obj + timedelta(hours=1)
                            event_end_time_str = end_time_obj.strftime("%H:%M")

                        event_datetime_str = f"{event_date_str}T{event_time_str}:00+09:00"
                        event_end_datetime_str = f"{event_date_str}T{event_end_time_str}:00+09:00"

                        pending_events.append({
                            'title': event_title,
                            'start_datetime': event_datetime_str,
                            'end_datetime': event_end_datetime_str,
                            'description': event_description
                        })

                import json
                self.db_helper.save_pending_event(line_user_id, json.dumps(pending_events))

                return TextSendMessage(text=response_text)

            # 全て重複なしの場合は成功メッセージのみ
            if auto_added_count > 0:
                # 1日分かつ1件の予定の場合は詳細を表示
                if len(auto_added_dates) == 1 and len(batch_events) == 1:
                    event_data = batch_events[0]
                    title = event_data.get('title', '予定')
                    start_datetime = event_data.get('start_datetime')
                    end_datetime = event_data.get('end_datetime')

                    # 日時をフォーマット
                    if start_datetime and end_datetime:
                        import pytz
                        jst = pytz.timezone('Asia/Tokyo')
                        start_dt = start_datetime.astimezone(jst)
                        end_dt = end_datetime.astimezone(jst)
                        weekday = "月火水木金土日"[start_dt.weekday()]

                        response_text = "✅予定を追加しました！\n\n"
                        response_text += f"📅{title}\n"
                        response_text += f"{start_dt.month}/{start_dt.day}（{weekday}）{start_dt.strftime('%H:%M')}〜{end_dt.strftime('%H:%M')}"
                        return TextSendMessage(text=response_text)

                # 複数日分の場合は簡素な表示
                formatted_dates = []
                for date_str in sorted(auto_added_dates):
                    dt = parser.parse(date_str)
                    formatted_dates.append(f"{dt.month}/{dt.day}")

                response_text = f"✅ {len(auto_added_dates)}日分の予定を追加しました！\n"
                response_text += f"（{', '.join(formatted_dates)}）"
                return TextSendMessage(text=response_text)

            # 予定追加処理（古いロジック - 実質的には使われない）
            for date_info in dates:
                try:
                    # 辞書でない場合はスキップ
                    if not isinstance(date_info, dict):
                        print(f"[WARNING] 古いロジック: date_infoが辞書でないためスキップ: {type(date_info)}")
                        continue

                    # 日時を構築
                    date_str = date_info.get('date')
                    time_str = date_info.get('time')
                    end_time_str = date_info.get('end_time')
                    title = date_info.get('title', '予定')
                    description = date_info.get('description', '')

                    if not date_str or not time_str:
                        print(f"[DEBUG] 不完全な予定情報をスキップ: {date_info}")
                        continue

                    # 終了時間が設定されていない場合は1時間後に設定
                    if not end_time_str or end_time_str == time_str:
                        from datetime import datetime, timedelta
                        time_obj = datetime.strptime(time_str, "%H:%M")
                        end_time_obj = time_obj + timedelta(hours=1)
                        end_time_str = end_time_obj.strftime("%H:%M")
                        print(f"[DEBUG] 終了時間を自動設定: {time_str} -> {end_time_str}")

                    # 日時文字列を構築
                    start_datetime_str = f"{date_str}T{time_str}:00+09:00"
                    end_datetime_str = f"{date_str}T{end_time_str}:00+09:00"

                    print(f"[DEBUG] 予定追加処理: {title} - {start_datetime_str} to {end_datetime_str}")

                    # 日時をパース
                    start_datetime = parser.parse(start_datetime_str)
                    end_datetime = parser.parse(end_datetime_str)

                    if start_datetime.tzinfo is None:
                        start_datetime = self.jst.localize(start_datetime)
                    if end_datetime.tzinfo is None:
                        end_datetime = self.jst.localize(end_datetime)

                    # 予定を追加
                    success, message, result = self.calendar_service.add_event(
                        title,
                        start_datetime,
                        end_datetime,
                        description,
                        line_user_id=line_user_id,
                        force_add=True
                    )
                    
                    if success:
                        # 元の表示形式に合わせて日時をフォーマット
                        from datetime import datetime
                        import pytz
                        jst = pytz.timezone('Asia/Tokyo')
                        start_dt = start_datetime.astimezone(jst)
                        end_dt = end_datetime.astimezone(jst)
                        weekday = "月火水木金土日"[start_dt.weekday()]
                        date_str = f"{start_dt.month}/{start_dt.day}（{weekday}）"
                        time_str = f"{start_dt.strftime('%H:%M')}〜{end_dt.strftime('%H:%M')}"
                        
                        added_events.append({
                            'title': title,
                            'time': f"{date_str}{time_str}"
                        })
                        print(f"[DEBUG] 予定追加成功: {title}")
                    else:
                        failed_events.append({
                            'title': title,
                            'time': f"{time_str}-{end_time_str}",
                            'reason': message
                        })
                        print(f"[DEBUG] 予定追加失敗: {title} - {message}")
                        
                except Exception as e:
                    print(f"[DEBUG] 予定処理エラー: {e}")
                    failed_events.append({
                        'title': date_info.get('title', '予定'),
                        'time': f"{date_info.get('time', '')}-{date_info.get('end_time', '')}",
                        'reason': str(e)
                    })
            
            # 結果メッセージを構築（移動時間を含む場合は統一形式）
            if added_events:
                # 移動時間が含まれているかチェック
                has_travel = any('移動時間' in event['title'] for event in added_events)
                
                if has_travel and len(added_events) > 1:
                    # 移動時間を含む場合は統一形式で表示
                    response_text = "✅予定を追加しました！\n\n"
                    
                    # 日付を取得（最初の予定から）
                    first_event = added_events[0]
                    time_str = first_event['time']
                    # "10/18 (土)19:00〜20:00" から "10/18 (土)" を抽出
                    date_match = re.search(r'(\d{1,2}/\d{1,2}\s*\([月火水木金土日]\)\s*)', time_str)
                    date_part = date_match.group(1).strip() if date_match else time_str
                    response_text += f"{date_part}\n"
                    response_text += "────────\n"
                    
                    # 時間順でソート（開始時間でソート）
                    def get_start_time(event):
                        time_str = event['time']
                        # "10/18 (土)19:00〜20:00" から "19:00〜20:00" を抽出
                        time_match = re.search(r'(\d{1,2}:\d{2}〜\d{1,2}:\d{2})', time_str)
                        time_part = time_match.group(1) if time_match else time_str
                        start_time = time_part.split('〜')[0]  # "19:00〜20:00" -> "19:00"
                        return start_time
                    
                    sorted_events = sorted(added_events, key=get_start_time)
                    
                    # 各予定を番号付きで表示
                    for i, event in enumerate(sorted_events, 1):
                        # 時間部分を抽出（"10:00~11:00" の形式）
                        # 日付と時間の区切りを正しく処理
                        time_str = event['time']
                        # "10/18 (土)19:00〜20:00" から "19:00〜20:00" を抽出
                        time_match = re.search(r'(\d{1,2}:\d{2}〜\d{1,2}:\d{2})', time_str)
                        time_part = time_match.group(1) if time_match else time_str
                        response_text += f"{i}. {event['title']}\n"
                        response_text += f"🕐 {time_part}\n"
                    
                    response_text += "────────"
                else:
                    # 通常の表示形式
                    response_text = "✅予定を追加しました！\n\n"
                    for event in added_events:
                        response_text += f"📅{event['title']}\n{event['time']}\n"
                
                if failed_events:
                    response_text += "\n\n⚠️追加できなかった予定:\n"
                    for event in failed_events:
                        response_text += f"• {event['title']} ({event['time']}) - {event['reason']}\n"
            else:
                response_text = "❌予定を追加できませんでした。\n\n"
                for event in failed_events:
                    response_text += f"• {event['title']} ({event['time']}) - {event['reason']}\n"
            
            return TextSendMessage(text=response_text)
            
        except Exception as e:
            print(f"[DEBUG] 複数予定処理エラー: {e}")
            return TextSendMessage(text=f"予定の処理中にエラーが発生しました: {str(e)}")

    def _handle_show_schedule(self, dates_info, line_user_id):
        """予定表示を処理します"""
        try:
            print(f"[DEBUG] _handle_show_schedule開始")
            print(f"[DEBUG] dates_info: {dates_info}")

            # ユーザーの認証状態をチェック
            if not self._check_user_auth(line_user_id):
                print(f"[DEBUG] ユーザー認証未完了")
                return self._send_auth_guide(line_user_id)

            if not self.calendar_service:
                print(f"[DEBUG] カレンダーサービス未初期化")
                return TextSendMessage(text="Google Calendarサービスが初期化されていません。")

            if not dates_info:
                print(f"[DEBUG] dates_infoが空")
                return TextSendMessage(text="日付を正しく認識できませんでした。")

            # 日付ごとに予定を取得
            from dateutil import parser
            import pytz
            jst = pytz.timezone('Asia/Tokyo')

            all_events = []
            for i, date_info in enumerate(dates_info):
                print(f"[DEBUG] date_info[{i}]のタイプ: {type(date_info)}, 値: {date_info}")

                # 文字列の場合はスキップ
                if isinstance(date_info, str):
                    print(f"[WARNING] date_info[{i}]が文字列のためスキップ: {date_info}")
                    continue

                # 辞書でない場合もスキップ
                if not isinstance(date_info, dict):
                    print(f"[WARNING] date_info[{i}]が辞書でないためスキップ: {type(date_info)}")
                    continue

                date_str = date_info.get('date')
                if not date_str:
                    print(f"[WARNING] date_info[{i}]にdateキーがない")
                    continue

                # その日の開始時刻と終了時刻
                start_datetime = jst.localize(datetime.strptime(f"{date_str} 00:00", "%Y-%m-%d %H:%M"))
                end_datetime = jst.localize(datetime.strptime(f"{date_str} 23:59", "%Y-%m-%d %H:%M"))

                # 予定を取得
                events = self.calendar_service.get_events_for_time_range(start_datetime, end_datetime, line_user_id)

                # 日付情報を追加
                for event in events:
                    event['date'] = date_str
                    all_events.append(event)

            # 予定をフォーマット
            if not all_events:
                return TextSendMessage(text="予定はありません。")

            # 日付ごとにグループ化
            from collections import defaultdict
            events_by_date = defaultdict(list)
            for event in all_events:
                events_by_date[event['date']].append(event)

            # レスポンステキストを構築
            response_text = "📅 予定一覧\n\n"

            for date_str in sorted(events_by_date.keys()):
                dt = parser.parse(date_str)
                weekday = "月火水木金土日"[dt.weekday()]
                response_text += f"【{dt.month}/{dt.day}（{weekday}）】\n"

                # その日の予定を時刻順にソート
                day_events = events_by_date[date_str]
                day_events.sort(key=lambda e: e.get('start', ''))

                for event in day_events:
                    title = event.get('title', '予定')
                    start_time = event.get('start', '')
                    end_time = event.get('end', '')

                    # 時刻をフォーマット
                    if 'T' in start_time:
                        start_dt = parser.parse(start_time)
                        end_dt = parser.parse(end_time)
                        start_dt = start_dt.astimezone(jst)
                        end_dt = end_dt.astimezone(jst)
                        time_str = f"{start_dt.strftime('%H:%M')}〜{end_dt.strftime('%H:%M')}"
                    else:
                        time_str = f"{start_time}〜{end_time}"

                    response_text += f"• {title}\n  {time_str}\n"

                response_text += "\n"

            return TextSendMessage(text=response_text.strip())

        except Exception as e:
            print(f"[DEBUG] _handle_show_scheduleで例外発生: {e}")
            import traceback
            traceback.print_exc()
            return TextSendMessage(text=f"予定表示でエラーが発生しました: {str(e)}")

    def _handle_availability_check(self, dates_info, line_user_id):
        """空き時間確認を処理します"""
        try:
            print(f"[DEBUG] _handle_availability_check開始")
            print(f"[DEBUG] dates_info: {dates_info}")
            print(f"[DEBUG] line_user_id: {line_user_id}")
            
            # ユーザーの認証状態をチェック
            if not self._check_user_auth(line_user_id):
                print(f"[DEBUG] ユーザー認証未完了")
                return self._send_auth_guide(line_user_id)
            
            if not self.calendar_service:
                print(f"[DEBUG] カレンダーサービス未初期化")
                return TextSendMessage(text="Google Calendarサービスが初期化されていません。認証ファイルを確認してください。")
            
            if not self.ai_service:
                print(f"[DEBUG] AIサービス未初期化")
                return TextSendMessage(text="AIサービスが初期化されていません。")
            
            if not dates_info:
                print(f"[DEBUG] dates_infoが空")
                return TextSendMessage(text="日付を正しく認識できませんでした。\n\n例: 「明日7/7 15:00〜15:30の空き時間を教えて」")
            
            print(f"[DEBUG] 空き時間計算開始")
            free_slots_by_frame = []
            for i, date_info in enumerate(dates_info):
                print(f"[DEBUG] 日付{i+1}処理開始: タイプ={type(date_info)}, 値={date_info}")

                # 文字列の場合はスキップ
                if isinstance(date_info, str):
                    print(f"[WARNING] date_info[{i}]が文字列のためスキップ: {date_info}")
                    continue

                # 辞書でない場合もスキップ
                if not isinstance(date_info, dict):
                    print(f"[WARNING] date_info[{i}]が辞書でないためスキップ: {type(date_info)}")
                    continue

                date_str = date_info.get('date')
                start_time = date_info.get('time')
                end_time = date_info.get('end_time')

                print(f"[DEBUG] 日付{i+1}の抽出値: date={date_str}, start_time={start_time}, end_time={end_time}")
                
                if date_str and start_time and end_time:
                    try:
                        jst = pytz.timezone('Asia/Tokyo')
                        start_dt = jst.localize(datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M"))
                        end_dt = jst.localize(datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M"))
                        
                        print(f"[DEBUG] 日付{i+1}のdatetime: start_dt={start_dt}, end_dt={end_dt}")
                        
                        # 枠内の予定を取得
                        print(f"[DEBUG] 日付{i+1}の予定取得開始")
                        events = self.calendar_service.get_events_for_time_range(start_dt, end_dt, line_user_id)
                        print(f"[DEBUG] 日付{i+1}の取得予定: {events}")
                        
                        # 8:00〜22:00の間で空き時間を返す
                        day_start = "08:00"
                        day_end = "22:00"
                        # 枠の範囲と8:00〜22:00の重なり部分だけを対象にする
                        slot_start = max(start_time, day_start)
                        slot_end = min(end_time, day_end)
                        
                        print(f"[DEBUG] 日付{i+1}のスロット範囲: slot_start={slot_start}, slot_end={slot_end}")
                        
                        slot_start_dt = jst.localize(datetime.strptime(f"{date_str} {slot_start}", "%Y-%m-%d %H:%M"))
                        slot_end_dt = jst.localize(datetime.strptime(f"{date_str} {slot_end}", "%Y-%m-%d %H:%M"))
                        
                        print(f"[DEBUG] 日付{i+1}のスロットdatetime: slot_start_dt={slot_start_dt}, slot_end_dt={slot_end_dt}")
                        
                        if slot_start < slot_end:
                            print(f"[DEBUG] 日付{i+1}の空き時間計算開始")
                            free_slots = self.calendar_service.find_free_slots_for_day(slot_start_dt, slot_end_dt, events)
                            print(f"[DEBUG] 日付{i+1}の空き時間結果: {free_slots}")
                        else:
                            print(f"[DEBUG] 日付{i+1}のスロット範囲が無効: {slot_start} >= {slot_end}")
                            free_slots = []
                        
                        free_slots_by_frame.append({
                            'date': date_str,
                            'start_time': slot_start,
                            'end_time': slot_end,
                            'free_slots': free_slots
                        })
                        print(f"[DEBUG] 日付{i+1}のfree_slots_by_frame追加完了")
                        
                    except Exception as e:
                        print(f"[DEBUG] 日付{i+1}処理でエラー: {e}")
                        import traceback
                        traceback.print_exc()
                        # エラーが発生しても他の日付は処理を続行
                        free_slots_by_frame.append({
                            'date': date_str,
                            'start_time': start_time,
                            'end_time': end_time,
                            'free_slots': []
                        })
                else:
                    print(f"[DEBUG] 日付{i+1}の必須項目が不足: date_str={date_str}, start_time={start_time}, end_time={end_time}")
            
            print(f"[DEBUG] 全日付処理完了、free_slots_by_frame: {free_slots_by_frame}")
            
            print(f"[DEBUG] format_free_slots_response_by_frame呼び出し")
            response_text = self.ai_service.format_free_slots_response_by_frame(free_slots_by_frame)
            print(f"[DEBUG] レスポンス生成完了: {response_text}")
            
            return TextSendMessage(text=response_text)
            
        except Exception as e:
            print(f"[DEBUG] _handle_availability_checkで例外発生: {e}")
            import traceback
            traceback.print_exc()
            return TextSendMessage(text=f"空き時間確認でエラーが発生しました: {str(e)}")
    
    def _handle_event_addition(self, user_message, line_user_id):
        """イベント追加を処理します"""
        try:
            # ユーザーの認証状態をチェック
            if not self._check_user_auth(line_user_id):
                return self._send_auth_guide(line_user_id)
            
            if not self.calendar_service:
                return TextSendMessage(text="Google Calendarサービスが初期化されていません。認証ファイルを確認してください。")
            
            if not self.ai_service:
                return TextSendMessage(text="AIサービスが初期化されていません。")
            
            # AIを使ってイベント情報を抽出
            event_info = self.ai_service.extract_event_info(user_message)
            
            if 'error' in event_info:
                # 日程のみの場合は空き時間確認として処理
                dates_info = self.ai_service.extract_dates_and_times(user_message)
                if 'error' not in dates_info and dates_info.get('dates'):
                    return self._handle_availability_check(dates_info.get('dates', []), line_user_id)
                
                return TextSendMessage(text="・日時を打つと空き時間を返します\n・予定を打つとカレンダーに追加します\n\n例：\n・「明日の空き時間」\n・「7/15 15:00〜16:00の空き時間」\n・「明日の午前9時から会議を追加して」\n・「来週月曜日の14時から打ち合わせ」")
            
            # 日時をパース
            start_datetime = parser.parse(event_info['start_datetime'])
            end_datetime = parser.parse(event_info['end_datetime'])
            
            # タイムゾーンを設定
            start_datetime = self.jst.localize(start_datetime)
            end_datetime = self.jst.localize(end_datetime)
            
            # カレンダーにイベントを追加
            success, message, result = self.calendar_service.add_event(
                event_info['title'],
                start_datetime,
                end_datetime,
                event_info.get('description', ''),
                line_user_id=line_user_id,
                force_add=True
            )
            logger.info(f"[DEBUG] add_event result: success={success}, message={message}, result={result}")
            
            # AIを使ってレスポンスをフォーマット
            response_text = self.ai_service.format_event_confirmation(success, message, result)
            
            return TextSendMessage(text=response_text)
            
        except Exception as e:
            return TextSendMessage(text=f"イベント追加でエラーが発生しました: {str(e)}")
    
    def get_handler(self):
        """WebhookHandlerを取得します"""
        return self.handler 