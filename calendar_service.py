from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import os
import pickle
import pytz
from config import Config
from dateutil import parser
from db import DBHelper
import logging

logger = logging.getLogger("calendar_service")
logger.setLevel(logging.INFO)
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

class GoogleCalendarService:
    def __init__(self):
        self.SCOPES = ['https://www.googleapis.com/auth/calendar']
        self.db_helper = DBHelper()
        self.creds = None
        self.service = None
        self._authenticate()
    
    def _authenticate(self):
        """Google Calendar APIの認証を行います"""
        # トークンファイルが存在する場合は読み込み
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                self.creds = pickle.load(token)
            # 有効な認証情報がない場合はWebフローで認証する（run_local_serverは呼ばない）
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
        else:
            self.creds = None
        
        if self.creds:
            self.service = build('calendar', 'v3', credentials=self.creds)
        else:
            self.service = None  # 認証情報がなければserviceはNoneのまま
    
    def _get_user_credentials(self, line_user_id):
        """ユーザーの認証トークンをDBから取得"""
        try:
            print(f"[DEBUG] _get_user_credentials開始: line_user_id={line_user_id}")
            token_data = self.db_helper.get_google_token(line_user_id)
            print(f"[DEBUG] DBから取得したトークンデータ: {token_data is not None}")
            
            if not token_data:
                print(f"[DEBUG] トークンデータが取得できませんでした")
                return None
            
            # トークンデータの型を確認
            print(f"[DEBUG] トークンデータの型: {type(token_data)}")
            if hasattr(token_data, '__len__'):
                print(f"[DEBUG] トークンデータの長さ: {len(token_data)}")
            
            credentials = None
            
            # まずpickle形式で試行
            try:
                print(f"[DEBUG] pickle形式のトークンデータのデシリアライズ開始")
                if isinstance(token_data, (bytes, bytearray)):
                    credentials = pickle.loads(token_data)
                else:
                    # memoryviewやその他の型の場合はbytesに変換
                    if hasattr(token_data, 'tobytes'):
                        token_bytes = token_data.tobytes()
                    else:
                        token_bytes = bytes(token_data)
                    credentials = pickle.loads(token_bytes)
                print(f"[DEBUG] pickle形式のトークンデータのデシリアライズ完了: credentials={credentials is not None}")
                
            except Exception as pickle_error:
                print(f"[DEBUG] pickle形式のトークン読み込みエラー: {pickle_error}")
                
                # JSON形式で試行
                try:
                    print(f"[DEBUG] JSON形式のトークンデータを試行")
                    import json
                    from google.oauth2.credentials import Credentials
                    
                    # トークンデータを文字列に変換
                    if isinstance(token_data, (bytes, bytearray)):
                        token_str = token_data.decode('utf-8')
                    elif hasattr(token_data, 'tobytes'):
                        token_str = token_data.tobytes().decode('utf-8')
                    else:
                        token_str = str(token_data)
                    
                    print(f"[DEBUG] JSON形式のトークンデータを取得")
                    token_dict = json.loads(token_str)
                    print(f"[DEBUG] JSON形式のトークンデータのパース完了")
                    
                    # Credentialsオブジェクトを作成
                    credentials = Credentials.from_authorized_user_info(token_dict)
                    print(f"[DEBUG] JSON形式からCredentialsオブジェクト作成完了")
                    
                except Exception as json_error:
                    print(f"[DEBUG] JSON形式のトークン読み込みエラー: {json_error}")
                    import traceback
                    traceback.print_exc()
                    return None
            
            if credentials:
                # トークンの有効期限をチェック
                if credentials.expired and credentials.refresh_token:
                    print(f"[DEBUG] トークンのリフレッシュ開始")
                    try:
                        credentials.refresh(Request())
                        print(f"[DEBUG] トークンのリフレッシュ完了")
                        # 更新されたトークンをDBに保存
                        updated_token_data = pickle.dumps(credentials)
                        self.db_helper.save_google_token(line_user_id, updated_token_data)
                        print(f"[DEBUG] 更新されたトークンをDBに保存完了")
                    except Exception as refresh_error:
                        print(f"[DEBUG] トークンのリフレッシュエラー: {refresh_error}")
                        # リフレッシュに失敗してもcredentialsは返す
                
                return credentials
            else:
                print(f"[DEBUG] 認証情報の作成に失敗しました")
                return None
                
        except Exception as e:
            print(f"[DEBUG] _get_user_credentialsで例外発生: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _get_calendar_service(self, line_user_id):
        """ユーザーごとのGoogle Calendarサービスを取得"""
        try:
            print(f"[DEBUG] _get_calendar_service開始: line_user_id={line_user_id}")
            credentials = self._get_user_credentials(line_user_id)
            print(f"[DEBUG] 認証情報取得結果: credentials={credentials is not None}")
            
            if not credentials:
                print(f"[DEBUG] 認証情報が取得できませんでした")
                raise Exception("ユーザーの認証トークンが見つかりません。認証を完了してください。")
            
            print(f"[DEBUG] Google Calendar APIサービス構築開始")
            service = build('calendar', 'v3', credentials=credentials)
            print(f"[DEBUG] Google Calendar APIサービス構築完了")
            return service
            
        except Exception as e:
            print(f"[DEBUG] _get_calendar_serviceで例外発生: {e}")
            import traceback
            traceback.print_exc()
            raise e
    
    def check_availability(self, start_time, end_time):
        """指定された時間帯の空き時間を確認します"""
        try:
            if not self.service:
                return None, "Google認証が必要です。"
            # ISO文字列がタイムゾーン付きかどうかでZを付与しない
            def iso_no_z(dt):
                s = dt.isoformat()
                return s if s.endswith(("+09:00", "+00:00", "-0")) else s + "Z"
            # 指定された時間帯のイベントを取得
            events_result = self.service.events().list(
                calendarId=Config.GOOGLE_CALENDAR_ID,  # 'primary'（各ユーザーのメインカレンダー）
                timeMin=iso_no_z(start_time),
                timeMax=iso_no_z(end_time),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])
            if not events:
                return True, "指定された時間帯は空いています。"
            # 既存のイベント情報を取得
            existing_events = []
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                end = event['end'].get('dateTime', event['end'].get('date'))
                title = event.get('summary', 'タイトルなし')
                existing_events.append({
                    'title': title,
                    'start': start,
                    'end': end
                })
            return False, existing_events
        except Exception as e:
            return None, f"エラーが発生しました: {str(e)}"
    
    def add_event(self, title, start_time, end_time, description="", line_user_id=None, force_add=False):
        """カレンダーにイベントを追加します"""
        try:
            if not line_user_id:
                return False, "ユーザーIDが必要です", None
            service = self._get_calendar_service(line_user_id)
            # 既存の予定をチェック（force_addがFalseのときのみ）
            if not force_add:
                events = self.get_events_for_time_range(start_time, end_time, line_user_id)
                logger.info(f"[DEBUG] add_event: 追加前に取得したevents = {events}")
                if events and len(events) > 0:
                    conflicting_events = []
                    for event in events:
                        if not isinstance(event, dict):
                            logger.warning(f"[WARN] add_event: eventがdict型でないためスキップ: {event}")
                            continue
                        conflicting_events.append({
                            'title': event.get('title', '予定なし'),
                            'start': event['start'].get('dateTime', event['start'].get('date')) if isinstance(event['start'], dict) else event['start'],
                            'end': event['end'].get('dateTime', event['end'].get('date')) if isinstance(event['end'], dict) else event['end']
                        })
                    logger.info(f"[DEBUG] 既存の予定があるため追加しません: {conflicting_events}")
                    if conflicting_events:
                        return False, "指定された時間に既存の予定があります", conflicting_events
            # イベントを作成
            event = {
                'summary': title,
                'description': description,
                'start': {
                    'dateTime': start_time.isoformat(),
                    'timeZone': 'Asia/Tokyo',
                },
                'end': {
                    'dateTime': end_time.isoformat(),
                    'timeZone': 'Asia/Tokyo',
                },
            }
            logger.info(f"[DEBUG] Google Calendar APIへイベント追加リクエスト: {event}")
            # イベントを追加
            event = service.events().insert(
                calendarId=Config.GOOGLE_CALENDAR_ID,  # 'primary'（各ユーザーのメインカレンダー）
                body=event
            ).execute()
            logger.info(f"[DEBUG] Google Calendar APIレスポンス: {event}")
            return True, "✅予定を追加しました", {
                'title': title,
                'start': start_time.isoformat(),
                'end': end_time.isoformat()
            }
        except Exception as e:
            logger.error(f"[ERROR] add_eventで例外発生: {e}")
            return False, f"エラーが発生しました: {str(e)}", None

    def add_events_batch(self, events_data, line_user_id=None, chunk_size=10):
        """複数のイベントを一度に追加します（Batch API使用、チャンク分割対応）

        Args:
            events_data: イベント情報のリスト [{'title': str, 'start_datetime': datetime, 'end_datetime': datetime, 'description': str}, ...]
            line_user_id: LINEユーザーID（認証トークン取得用）
            chunk_size: 1回のバッチリクエストで処理するイベント数（デフォルト: 10）

        Returns:
            (成功件数, 失敗件数, 詳細結果)
        """
        from googleapiclient.http import BatchHttpRequest

        try:
            # サービスを取得
            service = self._get_calendar_service(line_user_id)
            if not service:
                logger.error("[ERROR] カレンダーサービスの取得に失敗")
                return 0, len(events_data), []

            # 全体の結果を格納
            total_results = {
                'success': [],
                'failed': []
            }

            # イベントをチャンクに分割
            total_events = len(events_data)
            num_chunks = (total_events + chunk_size - 1) // chunk_size  # 切り上げ除算

            logger.info(f"[DEBUG] Batch API実行開始: 全{total_events}件を{num_chunks}チャンク（{chunk_size}件ずつ）に分割して処理")

            # チャンクごとに処理
            for chunk_idx in range(num_chunks):
                start_idx = chunk_idx * chunk_size
                end_idx = min(start_idx + chunk_size, total_events)
                chunk_events = events_data[start_idx:end_idx]

                logger.info(f"[DEBUG] チャンク {chunk_idx + 1}/{num_chunks} 処理開始: {start_idx + 1}〜{end_idx}件目（{len(chunk_events)}件）")

                # バッチリクエストを作成
                batch = service.new_batch_http_request()

                # チャンク用の結果
                chunk_results = {
                    'success': [],
                    'failed': []
                }

                def make_callback(chunk_results):
                    """クロージャを使ってchunk_resultsをキャプチャ"""
                    def callback(request_id, response, exception):
                        """バッチリクエストのコールバック"""
                        if exception is not None:
                            logger.error(f"[ERROR] Batch request {request_id} failed: {exception}")
                            chunk_results['failed'].append({
                                'request_id': request_id,
                                'error': str(exception)
                            })
                        else:
                            logger.info(f"[DEBUG] Batch request {request_id} success: {response.get('summary', 'No title')}")
                            chunk_results['success'].append({
                                'request_id': request_id,
                                'event': response
                            })
                    return callback

                # バッチにリクエストを追加
                for idx, event_data in enumerate(chunk_events):
                    event = {
                        'summary': event_data['title'],
                        'description': event_data.get('description', ''),
                        'start': {
                            'dateTime': event_data['start_datetime'].isoformat(),
                            'timeZone': 'Asia/Tokyo',
                        },
                        'end': {
                            'dateTime': event_data['end_datetime'].isoformat(),
                            'timeZone': 'Asia/Tokyo',
                        },
                    }

                    batch.add(
                        service.events().insert(
                            calendarId=Config.GOOGLE_CALENDAR_ID,
                            body=event
                        ),
                        callback=make_callback(chunk_results),
                        request_id=f"{chunk_idx}_{idx}"
                    )

                # バッチリクエストを実行
                batch.execute()

                # チャンクの結果を全体に集計
                total_results['success'].extend(chunk_results['success'])
                total_results['failed'].extend(chunk_results['failed'])

                logger.info(f"[DEBUG] チャンク {chunk_idx + 1}/{num_chunks} 完了: 成功={len(chunk_results['success'])}件, 失敗={len(chunk_results['failed'])}件")

            success_count = len(total_results['success'])
            failed_count = len(total_results['failed'])

            logger.info(f"[DEBUG] Batch API全体完了: 成功={success_count}件, 失敗={failed_count}件")

            return success_count, failed_count, total_results

        except Exception as e:
            logger.error(f"[ERROR] add_events_batchで例外発生: {e}")
            import traceback
            traceback.print_exc()
            return 0, len(events_data), {'error': str(e)}

    def get_events_for_dates(self, dates, line_user_id=None):
        """指定された日付のイベントを取得します（ユーザーごとの認証トークン対応、JST日付で正確に抽出）"""
        import pytz
        events_info = []
        jst = pytz.timezone('Asia/Tokyo')
        for date in dates:
            # JST 0:00〜翌日0:00をUTCに変換
            start_of_day_jst = jst.localize(datetime.combine(date, datetime.min.time()))
            end_of_day_jst = start_of_day_jst + timedelta(days=1)
            start_of_day_utc = start_of_day_jst.astimezone(pytz.UTC)
            end_of_day_utc = end_of_day_jst.astimezone(pytz.UTC)
            try:
                service = self._get_calendar_service(line_user_id) if line_user_id else self.service
                if not service:
                    events_info.append({
                        'date': date.strftime('%Y-%m-%d'),
                        'events': [],
                        'error': 'Google認証が必要です。'
                    })
                    continue
                events_result = service.events().list(
                    calendarId=Config.GOOGLE_CALENDAR_ID,
                    timeMin=start_of_day_utc.isoformat(),
                    timeMax=end_of_day_utc.isoformat(),
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
                print(f"[DEBUG] Google Calendar APIレスポンス: {events_result}")
                events = events_result.get('items', [])
                if events:
                    day_events = []
                    for event in events:
                        start = event['start'].get('dateTime', event['start'].get('date'))
                        end = event['end'].get('dateTime', event['end'].get('date'))
                        title = event.get('summary', 'タイトルなし')
                        day_events.append({
                            'title': title,
                            'start': start,
                            'end': end
                        })
                    events_info.append({
                        'date': date.strftime('%Y-%m-%d'),
                        'events': day_events
                    })
                else:
                    events_info.append({
                        'date': date.strftime('%Y-%m-%d'),
                        'events': []
                    })
            except Exception as e:
                events_info.append({
                    'date': date.strftime('%Y-%m-%d'),
                    'error': str(e)
                })
        return events_info
    
    def get_events_for_time_range(self, start_time, end_time, line_user_id):
        """指定された時間範囲のイベントを取得します（全てのカレンダーから取得）"""
        try:
            print(f"[DEBUG] get_events_for_time_range開始")
            print(f"[DEBUG] 入力: start_time={start_time}, end_time={end_time}, line_user_id={line_user_id}")

            jst = pytz.timezone('Asia/Tokyo')
            # タイムゾーンなしならJSTを付与
            if start_time.tzinfo is None:
                start_time = jst.localize(start_time)
            if end_time.tzinfo is None:
                end_time = jst.localize(end_time)

            print(f"[DEBUG] タイムゾーン調整後: start_time={start_time}, end_time={end_time}")

            service = self._get_calendar_service(line_user_id)
            print(f"[DEBUG] カレンダーサービス取得完了")

            # タイムゾーンをUTCに変換
            utc_start = start_time.astimezone(pytz.UTC)
            utc_end = end_time.astimezone(pytz.UTC)

            print(f"[DEBUG] UTC変換後: utc_start={utc_start}, utc_end={utc_end}")

            # 全てのカレンダーを取得
            try:
                calendar_list = service.calendarList().list().execute()
                calendars = calendar_list.get('items', [])
                print(f"[DEBUG] 利用可能なカレンダー数: {len(calendars)}")
                for cal in calendars:
                    print(f"[DEBUG]   - {cal.get('summary', '不明')} (ID: {cal.get('id')})")
            except Exception as e:
                print(f"[DEBUG] カレンダーリスト取得エラー: {e}")
                calendars = [{'id': Config.GOOGLE_CALENDAR_ID}]

            # 全てのカレンダーから予定を取得
            all_events = []
            for calendar in calendars:
                calendar_id = calendar.get('id')
                calendar_name = calendar.get('summary', calendar_id)

                try:
                    print(f"[DEBUG] カレンダー '{calendar_name}' から予定取得中...")
                    events_result = service.events().list(
                        calendarId=calendar_id,
                        timeMin=utc_start.isoformat(),
                        timeMax=utc_end.isoformat(),
                        singleEvents=True,
                        orderBy='startTime'
                    ).execute()

                    events = events_result.get('items', [])
                    print(f"[DEBUG]   → {len(events)}件の予定を取得")
                    all_events.extend(events)
                except Exception as e:
                    print(f"[DEBUG] カレンダー '{calendar_name}' からの予定取得エラー: {e}")
                    continue

            print(f"[DEBUG] 全カレンダーから取得した予定総数: {len(all_events)}")

            # 時刻順にソート
            all_events = sorted(all_events, key=lambda e: e['start'].get('dateTime', e['start'].get('date', '')))

            if not all_events:
                print(f"[DEBUG] 全カレンダーを検索したがイベントなし")
                return []

            event_list = []
            for i, event in enumerate(all_events):
                print(f"[DEBUG] イベント{i+1}処理: {event.get('summary', 'タイトルなし')}")
                start = event['start'].get('dateTime', event['start'].get('date'))
                end = event['end'].get('dateTime', event['end'].get('date'))
                title = event.get('summary', 'タイトルなし')

                event_data = {
                    'title': title,
                    'start': start,
                    'end': end
                }
                event_list.append(event_data)
                print(f"[DEBUG]   時間: {start} 〜 {end}")

            print(f"[DEBUG] 最終イベントリスト: {len(event_list)}件")
            return event_list
            
        except Exception as e:
            print(f"[DEBUG] get_events_for_time_rangeで例外発生: {e}")
            import traceback
            traceback.print_exc()
            logging.error(f"イベント取得エラー: {e}")
            return []
    
    def find_free_slots_for_day(self, start_dt, end_dt, events):
        """指定枠(start_dt, end_dt)内で既存予定を除いた空き時間帯リストを返す"""
        try:
            logger.info(f"[DEBUG] find_free_slots_for_day開始")
            logger.info(f"[DEBUG] 検索枠: {start_dt} 〜 {end_dt}")
            logger.info(f"[DEBUG] 既存予定数: {len(events) if events else 0}")
            
            jst = pytz.timezone('Asia/Tokyo')
            if start_dt.tzinfo is None:
                start_dt = jst.localize(start_dt)
            if end_dt.tzinfo is None:
                end_dt = jst.localize(end_dt)
                
            # eventsがNoneや空の場合は必ず再取得
            if events is None or len(events) == 0:
                logger.info(f"[DEBUG] 既存予定なし、全日空き時間として返す")
                return [{
                    'start': start_dt.strftime('%H:%M'),
                    'end': end_dt.strftime('%H:%M')
                }]
                
            # 既存予定を時間順にbusy_timesへ
            busy_times = []
            logger.info(f"[DEBUG] 既存予定の処理開始")
            
            for i, event in enumerate(events):
                logger.info(f"[DEBUG] 予定{i+1}: {event}")
                
                start = event['start'] if isinstance(event['start'], str) else event['start'].get('dateTime', event['start'].get('date'))
                end = event['end'] if isinstance(event['end'], str) else event['end'].get('dateTime', event['end'].get('date'))
                
                logger.info(f"[DEBUG] 予定{i+1}の時間: {start} 〜 {end}")
                
                if 'T' in start:  # dateTime形式
                    start_ev = datetime.fromisoformat(start.replace('Z', '+00:00'))
                    end_ev = datetime.fromisoformat(end.replace('Z', '+00:00'))
                    
                    logger.info(f"[DEBUG] 予定{i+1}のパース後: {start_ev} 〜 {end_ev}")
                    
                    # 枠外の予定は除外
                    if end_ev <= start_dt or start_ev >= end_dt:
                        logger.info(f"[DEBUG] 予定{i+1}は枠外のため除外")
                        continue
                        
                    busy_start = max(start_ev, start_dt)
                    busy_end = min(end_ev, end_dt)
                    busy_times.append((busy_start, busy_end))
                    logger.info(f"[DEBUG] 予定{i+1}をbusy_timesに追加: {busy_start} 〜 {busy_end}")
                    
                else:  # date型（終日予定）
                    allday_start = jst.localize(datetime.combine(datetime.strptime(start, "%Y-%m-%d"), datetime.min.time()))
                    allday_end = allday_start + timedelta(days=1)
                    
                    logger.info(f"[DEBUG] 予定{i+1}は終日予定: {allday_start} 〜 {allday_end}")
                    
                    if allday_end <= start_dt or allday_start >= end_dt:
                        logger.info(f"[DEBUG] 予定{i+1}は枠外のため除外")
                        continue
                        
                    busy_start = max(allday_start, start_dt)
                    busy_end = min(allday_end, end_dt)
                    busy_times.append((busy_start, busy_end))
                    logger.info(f"[DEBUG] 予定{i+1}をbusy_timesに追加: {busy_start} 〜 {busy_end}")
            
            logger.info(f"[DEBUG] busy_times: {busy_times}")
            
            # 空き時間を計算
            free_slots = []
            if not busy_times:
                logger.info(f"[DEBUG] busy_timesが空、全日空き時間として返す")
                free_slots.append({
                    'start': start_dt.strftime('%H:%M'),
                    'end': end_dt.strftime('%H:%M')
                })
                return free_slots
                
            # busy_timesを開始時刻順に明示的にソート
            busy_times = sorted(busy_times, key=lambda x: x[0])
            logger.info(f"[DEBUG] ソート後のbusy_times: {busy_times}")
            
            current_time = start_dt
            logger.info(f"[DEBUG] 空き時間計算開始、current_time: {current_time}")
            
            for i, (busy_start, busy_end) in enumerate(busy_times):
                logger.info(f"[DEBUG] busy_times[{i}]処理: {busy_start} 〜 {busy_end}")
                
                if current_time < busy_start:
                    free_slot = {
                        'start': current_time.strftime('%H:%M'),
                        'end': busy_start.strftime('%H:%M')
                    }
                    free_slots.append(free_slot)
                    logger.info(f"[DEBUG] 空き時間を追加: {free_slot}")
                    
                current_time = max(current_time, busy_end)
                logger.info(f"[DEBUG] current_time更新: {current_time}")
                
            if current_time < end_dt:
                free_slot = {
                    'start': current_time.strftime('%H:%M'),
                    'end': end_dt.strftime('%H:%M')
                }
                free_slots.append(free_slot)
                logger.info(f"[DEBUG] 最後の空き時間を追加: {free_slot}")
                
            logger.info(f"[DEBUG] 最終的な空き時間: {free_slots}")
            return free_slots 
            
        except Exception as e:
            logger.error(f"空き時間検索エラー: {e}")
            return [] 