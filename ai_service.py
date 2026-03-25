import openai
from datetime import datetime, timedelta
from dateutil import parser
import re
import json
from config import Config
import calendar
import pytz
import logging

logger = logging.getLogger("ai_service")
logger.setLevel(logging.INFO)
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

class AIService:
    def __init__(self):
        self.client = openai.OpenAI(api_key=Config.OPENAI_API_KEY)
    
    def _get_jst_now_str(self):
        now = datetime.now(pytz.timezone('Asia/Tokyo'))
        return now.strftime('%Y-%m-%dT%H:%M:%S%z')
    
    def extract_dates_and_times(self, text, conversation_history=None):
        """テキストから日時を抽出し、タスクの種類を判定します

        Args:
            text: ユーザーのメッセージ
            conversation_history: 会話履歴 [{'role': 'user'/'assistant', 'content': '...'}]
        """
        try:
            now_jst = self._get_jst_now_str()
            system_prompt = (
                f"あなたはスケジュール管理アシスタントです。現在は {now_jst} です。\n\n"
                "【最重要】会話履歴がある場合は、必ず履歴の文脈を考慮してユーザーの意図を判断してください。\n"
                "過去のやり取りで言及された内容や文脈を踏まえて、現在のメッセージを解釈してください。\n\n"
                "【重要】ユーザーのメッセージを理解し、必ず以下のJSON形式のみで返してください。説明文は不要です。\n\n"
                "タスクタイプ:\n"
                "- show_schedule: 予定の表示・確認（「予定教えて」「何がある？」「スケジュール見せて」など、明示的に予定を見たい場合）\n"
                "- availability_check: 空き時間確認（「空き時間」「空いてる時間」「いつ空いてる」などのキーワードがある場合、または日時のみでタイトルなし）\n"
                "- add_event: 予定追加（日時+タイトルがある場合）\n\n"
                "【判断基準の優先順位】\n"
                "1. 「空き時間」「空いてる」「フリー」などのキーワードがある → availability_check（最優先）\n"
                "2. タイトル/内容がある → add_event\n"
                "3. 「予定」「スケジュール」「何がある」などの質問で、空き時間を聞いていない → show_schedule\n"
                "4. 日時のみで空き時間も聞いていない → availability_check\n\n"
                "日付・時刻の解釈:\n"
                "- 「明日」「来週」「X月Y週目」などの自然言語を適切に日付に変換\n"
                "- 「来週」は次の月曜から日曜の7日間\n"
                "- 「X月Y週目」は7日区切り（1週目=1-7日、2週目=8-14日、3週目=15-21日、4週目=22日以降）を各日のdatesとして返す\n"
                "- 「X時以降」はX:00〜23:59\n"
                "- 「午前中」は08:00〜12:00、「午後」は12:00〜18:00\n"
                "- 「4/1.2.3」のようなドット区切りは各日付として認識（月/日1.日2.日3の形式）\n"
                "- 複数日に時刻条件がある場合、各日に同じ時刻を適用\n"
                "- **重要**: 空き時間確認では、1日につき1つのエントリ（08:00〜22:00）を返す。1時間ごとに分割しない。\n\n"
                "必要な時間の長さの解釈:\n"
                "- 「X時間の打ち合わせ」「X時間の予定」→ required_duration_minutes に X*60 を設定\n"
                "- 「X分の打ち合わせ」「X分の予定」→ required_duration_minutes に X を設定\n"
                "- 「1時間打ち合わせできる日」→ required_duration_minutes: 60\n"
                "- 「2時間空いてる日」→ required_duration_minutes: 120\n"
                "- 時間の長さが指定されていない場合は required_duration_minutes を省略\n\n"
                "【必須】出力JSON形式（datesは必ず配列で返す）:\n"
                '{\n  "task_type": "show_schedule/availability_check/add_event",\n  "dates": [{"date": "YYYY-MM-DD", "time": "HH:MM", "end_time": "HH:MM", "title": "タイトル(add_eventのみ)"}],\n  "required_duration_minutes": 60 (オプション: 「X時間の打ち合わせ」などで指定された場合のみ)\n}\n\n'
                "具体例:\n"
                "・「3/29 7:00~8:00 フランクリン」→ {\"task_type\": \"add_event\", \"dates\": [{\"date\": \"2026-03-29\", \"time\": \"07:00\", \"end_time\": \"08:30\", \"title\": \"フランクリン\"}]}\n"
                "・「4/8.14.23 6:00~8:30 TSP」→ 3日分のdatesを返す（各日同じ時間とタイトル）\n"
                "・「明日の空き時間」→ {\"task_type\": \"availability_check\", \"dates\": [{\"date\": \"2026-03-26\", \"time\": \"08:00\", \"end_time\": \"22:00\"}]}\n"
                "・「3月で1時間打ち合わせできる日」→ {\"task_type\": \"availability_check\", \"dates\": [{\"date\": \"2026-03-01\", \"time\": \"08:00\", \"end_time\": \"22:00\"}, {\"date\": \"2026-03-02\", \"time\": \"08:00\", \"end_time\": \"22:00\"}, ... {\"date\": \"2026-03-31\", \"time\": \"08:00\", \"end_time\": \"22:00\"}], \"required_duration_minutes\": 60}\n"
                "・「来週2時間空いてる日」→ {\"task_type\": \"availability_check\", \"dates\": [来週の7日分], \"required_duration_minutes\": 120}\n"
                "・「4月1週目の午後の空き時間」→ {\"task_type\": \"availability_check\", \"dates\": [{\"date\": \"2026-04-01\", \"time\": \"12:00\", \"end_time\": \"18:00\"}, {\"date\": \"2026-04-02\", \"time\": \"12:00\", \"end_time\": \"18:00\"}, ... {\"date\": \"2026-04-07\", \"time\": \"12:00\", \"end_time\": \"18:00\"}]}\n"
                "・「明日の予定教えて」→ {\"task_type\": \"show_schedule\", \"dates\": [{\"date\": \"2026-03-26\"}]}\n"
                "・「4月1週目の予定」→ {\"task_type\": \"show_schedule\", \"dates\": [{\"date\": \"2026-04-01\"}, {\"date\": \"2026-04-02\"}, ... {\"date\": \"2026-04-07\"}]}\n\n"
                "【注意事項】\n"
                "- 「空き時間」「空いてる」「フリー」というキーワードがあれば必ず availability_check\n"
                "- タイトル/内容がある → add_event\n"
                "- 日時のみでキーワードなし → availability_check\n"
                "- 「予定」「スケジュール」などの質問で、空き時間のキーワードがない → show_schedule\n"
                "- 必ずdatesキー（配列）を使用\n"
                "- 「移動時間X時間」がある場合は \"travel_time_hours\": X を追加\n"
                "- 「X時間の打ち合わせ」「X時間空いてる」などの表現があれば、必ず \"required_duration_minutes\" を追加\n"
                "- **絶対に守る**: 空き時間確認では、同じ日付に対して複数のエントリを返さない。1日につき1エントリのみ。\n"
                "- **絶対に守る**: 1時間ごとに分割しない。連続した時間帯は1つのエントリにまとめる。\n\n"
                "【最重要】\n"
                "1. 「空き時間」というキーワードがある場合は、どんな状況でも availability_check を選択してください。\n"
                "2. 会話履歴（過去のメッセージ）の文脈を必ず考慮してください。前回のやり取りで言及された内容や日時を参照してください。\n"
                "3. 現在のメッセージと会話履歴を組み合わせて、ユーザーの真の意図を理解してください。\n"
                "4. 会話履歴に基づいて、省略された情報（日時、タイトルなど）を補完してください。\n"
                "5. 「X時間の打ち合わせ」「X時間空いてる」などの時間要件を必ず抽出して required_duration_minutes に設定してください。"
            )

            # メッセージ構築（会話履歴を含める）
            messages = [{"role": "system", "content": system_prompt}]

            # 会話履歴を追加（最新5件まで）
            if conversation_history:
                logger.info(f"[DEBUG] 会話履歴を追加: {len(conversation_history)}件（最新5件まで使用）")
                for i, msg in enumerate(conversation_history[-5:]):
                    logger.info(f"[DEBUG] 会話履歴[{i}]: role={msg['role']}, content={msg['content'][:50]}...")
                    messages.append({
                        "role": msg['role'],
                        "content": msg['content']
                    })
            else:
                logger.info(f"[DEBUG] 会話履歴なし（初回メッセージまたは履歴なし）")

            # 現在のユーザーメッセージを追加
            messages.append({
                "role": "user",
                "content": text
            })

            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.1
            )
            result = response.choices[0].message.content
            logger.info(f"[DEBUG] AI生レスポンス: {result}")
            parsed = self._parse_ai_response(result)

            # AIの判定を尊重
            logger.info(f"[DEBUG] パース後のJSON: {parsed}")
            logger.info(f"[DEBUG] AIが判定したtask_type: {parsed.get('task_type')}")
            logger.info(f"[DEBUG] AIが返したdates数: {len(parsed.get('dates', []))}件")

            # 同じ日付が複数ある場合は警告
            if 'dates' in parsed:
                date_counts = {}
                for d in parsed['dates']:
                    if isinstance(d, dict) and 'date' in d:
                        date_str = d['date']
                        date_counts[date_str] = date_counts.get(date_str, 0) + 1

                duplicates = {date: count for date, count in date_counts.items() if count > 1}
                if duplicates:
                    logger.warning(f"[WARNING] AIが同じ日付を複数回返しました: {duplicates}")
                    for date, count in duplicates.items():
                        logger.warning(f"[WARNING]   {date}: {count}回")

            # 'date'キーがあり'dates'がない場合、'dates'配列に変換
            if 'date' in parsed and 'dates' not in parsed:
                date_value = parsed['date']
                logger.warning(f"[WARNING] AIが'date'キーで返答。'dates'配列に変換します: {date_value}")
                parsed['dates'] = [{'date': date_value}]
                del parsed['date']  # 重複を避けるため削除

            if 'dates' in parsed:
                logger.info(f"[DEBUG] datesの内容: {parsed['dates']}")
                for i, d in enumerate(parsed.get('dates', [])):
                    logger.info(f"[DEBUG] dates[{i}]: タイプ={type(d)}, 値={d}")

            # _supplement_times関数を呼び出して日時の補完処理を実施
            try:
                parsed = self._supplement_times(parsed, text)
                logger.info(f"[DEBUG] _supplement_times処理後: {parsed}")
            except Exception as supplement_error:
                logger.error(f"[ERROR] _supplement_times処理エラー: {supplement_error}")
                import traceback
                traceback.print_exc()
                # エラーが発生しても、AIの解析結果をそのまま返す
                logger.warning(f"[WARNING] _supplement_timesエラーのため、AI解析結果をそのまま使用")

            return parsed

        except Exception as e:
            logger.error(f"[ERROR] extract_dates_and_times全体エラー: {e}")
            import traceback
            traceback.print_exc()
            return {"error": "イベント情報を正しく認識できませんでした。\n\n・日時を打つと空き時間を返します\n・予定を打つとカレンダーに追加します\n\n例：\n『明日の午前9時から会議を追加して』\n『来週月曜日の14時から打ち合わせ』"}
    
    def _parse_ai_response(self, response):
        """AIの応答をパースします"""
        try:
            # JSON部分を抽出
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            else:
                return {"error": "AI応答のパースに失敗しました"}
        except Exception as e:
            return {"error": f"JSONパースエラー: {str(e)}"}
    
    def _supplement_times(self, parsed, original_text):
        from datetime import datetime, timedelta
        import re
        jst = pytz.timezone('Asia/Tokyo')
        now = datetime.now(jst)
        logger = logging.getLogger("ai_service")
        print(f"[DEBUG] _supplement_times開始: parsed={parsed}")
        print(f"[DEBUG] 元テキスト: {original_text}")

        # parsedの検証
        if not parsed or not isinstance(parsed, dict):
            print(f"[DEBUG] parsedが無効: {parsed}")
            return parsed

        if 'dates' not in parsed or not isinstance(parsed.get('dates'), list):
            print(f"[DEBUG] datesが存在しないか無効: {parsed}")
            return parsed

        # dates配列の各要素が辞書であることを検証
        valid_dates = []
        for i, d in enumerate(parsed['dates']):
            if isinstance(d, dict):
                valid_dates.append(d)
            else:
                print(f"[WARNING] dates[{i}]が辞書でないためスキップ: タイプ={type(d)}, 値={d}")

        if not valid_dates:
            print(f"[DEBUG] 有効なdatesエントリが存在しない")
            return parsed

        # 有効な辞書のみを使用
        parsed['dates'] = valid_dates
        allday_dates = set()
        new_dates = []

        # 「X月Y週目」が元のテキストに含まれているかチェック（ループ前に処理）
        week_match = re.search(r'(\d{1,2})月(\d+)週目', original_text)
        if week_match:
            target_month = int(week_match.group(1))
            target_week = int(week_match.group(2))

            # 指定月の1日を取得
            target_year = now.year
            # 月が現在より前なら来年とする
            if target_month < now.month:
                target_year += 1

            from datetime import datetime
            month_start = datetime(target_year, target_month, 1)

            # Y週目の開始日と終了日を計算（1週目=1-7日、2週目=8-14日、...）
            week_start_day = (target_week - 1) * 7 + 1
            week_end_day = min(target_week * 7, calendar.monthrange(target_year, target_month)[1])

            # AIが既に週を展開済みかチェック
            ai_dates_in_target_week = [
                d for d in parsed['dates']
                if d.get('date') and d.get('date').startswith(f"{target_year}-{target_month:02d}")
            ]
            ai_already_expanded_week = len(ai_dates_in_target_week) >= 5

            if not ai_already_expanded_week:
                # AIが展開していない場合のみ、週の各日付を生成
                week_dates = []
                for day in range(week_start_day, week_end_day + 1):
                    week_date = datetime(target_year, target_month, day)
                    week_dates.append({'date': week_date.strftime('%Y-%m-%d')})

                print(f"[DEBUG] {target_month}月{target_week}週目を展開: {len(week_dates)}日分")
                parsed['dates'] = week_dates
            else:
                print(f"[DEBUG] AI既展開の{target_month}月{target_week}週目: {len(ai_dates_in_target_week)}日分")

        # 「来週」が元のテキストに含まれているかチェック
        has_next_week = re.search(r'来週', original_text) is not None
        if has_next_week:
            # 来週の月曜日を計算
            days_until_next_monday = (7 - now.weekday()) % 7
            if days_until_next_monday == 0:  # 今日が月曜日の場合
                days_until_next_monday = 7
            next_monday = now + timedelta(days=days_until_next_monday)
            next_sunday = next_monday + timedelta(days=6)

            # AI応答に既に来週の範囲の日付が複数含まれているかチェック
            dates_in_next_week = [
                d for d in parsed['dates']
                if d.get('date') and next_monday.strftime('%Y-%m-%d') <= d.get('date') <= next_sunday.strftime('%Y-%m-%d')
            ]

            # 既に5日以上あれば、AIが来週を展開済みと判断
            ai_already_expanded_next_week = len(dates_in_next_week) >= 5
            print(f"[DEBUG] 来週検出: AI既展開={ai_already_expanded_next_week}, 該当日数={len(dates_in_next_week)}")
        else:
            ai_already_expanded_next_week = False

        # 1. AI抽出を最優先。time, end_timeが空欄のものだけ補完
        for d in parsed['dates']:
            print(f"[DEBUG] datesループ: {d}")
            phrase = d.get('description', '') or original_text

            # まず「X時以降」を最優先でチェック（早期リターンより前に処理）
            time_after_match = re.search(r'(\d{1,2})時以降', original_text) or re.search(r'(\d{1,2})時以降', phrase)
            if time_after_match:
                hour = int(time_after_match.group(1))
                # timeが未設定の場合は設定
                if not d.get('time'):
                    d['time'] = f"{hour:02d}:00"
                # end_timeは常に23:59に強制設定（AIが誤って18:00などに設定していても上書き）
                d['end_time'] = '23:59'
                print(f"[DEBUG] X時以降を検出し強制設定: {hour}時以降 -> time={d['time']}, end_time={d['end_time']}")

            # 「来週」「来月」などの複数日展開が必要なキーワードをチェック
            needs_multi_day_expansion = (
                re.search(r'来週', phrase) or
                re.search(r'来週', original_text) or
                re.search(r'来月', phrase) or
                re.search(r'来月', original_text) or
                re.search(r'\d{1,2}月(?!.*日)', phrase)  # 「X月」（日付なし）
            )

            # time, end_timeが両方セットされていて、かつ複数日展開が不要な場合はそのまま追加
            if d.get('time') and d.get('end_time') and not needs_multi_day_expansion:
                new_dates.append(d)
                continue

            # time, end_timeが空欄の場合のみ補完
            # 範囲表現
            range_match = re.search(r'(\d{1,2})[\-〜~](\d{1,2})時', phrase)
            if range_match:
                d['time'] = f"{int(range_match.group(1)):02d}:00"
                d['end_time'] = f"{int(range_match.group(2)):02d}:00"
            # 終日
            if (not d.get('time') and not d.get('end_time')) or re.search(r'終日', phrase):
                d['time'] = '00:00'
                d['end_time'] = '23:59'
                if d.get('date') in allday_dates:
                    print(f"[DEBUG] 同じ日付の終日予定はスキップ: {d.get('date')}")
                    continue
                allday_dates.add(d.get('date'))
            # 明日
            if re.search(r'明日', phrase):
                d['date'] = (now + timedelta(days=1)).strftime('%Y-%m-%d')
                if not d.get('time'):
                    d['time'] = '08:00'
                if not d.get('end_time'):
                    d['end_time'] = '22:00'
            # 今日
            if re.search(r'今日', phrase):
                d['date'] = now.strftime('%Y-%m-%d')
                # 今日X時の形式を処理
                time_match = re.search(r'今日(\d{1,2})時', phrase)
                if time_match:
                    hour = int(time_match.group(1))
                    d['time'] = f"{hour:02d}:00"
                    d['end_time'] = f"{hour+1:02d}:00"
                elif not d.get('time'):
                    d['time'] = now.strftime('%H:%M')
                # 今日の場合は終了時間を1時間後に強制設定（AIの設定を上書き）
                if d.get('time'):
                    from datetime import datetime, timedelta
                    time_obj = datetime.strptime(d.get('time'), "%H:%M")
                    end_time_obj = time_obj + timedelta(hours=1)
                    d['end_time'] = end_time_obj.strftime('%H:%M')
                    print(f"[DEBUG] 今日の終了時間を1時間後に強制設定: {d.get('time')} -> {d['end_time']}")
            # 本日
            if re.search(r'本日', phrase):
                d['date'] = now.strftime('%Y-%m-%d')
                # 本日X時の形式を処理
                time_match = re.search(r'本日(\d{1,2})時', phrase)
                if time_match:
                    hour = int(time_match.group(1))
                    d['time'] = f"{hour:02d}:00"
                    d['end_time'] = f"{hour+1:02d}:00"
                    print(f"[DEBUG] 本日X時の処理: {hour}時 -> {hour+1}時")
                elif not d.get('time'):
                    d['time'] = now.strftime('%H:%M')
                # 本日の場合は終了時間を1時間後に強制設定（AIの設定を上書き）
                if d.get('time'):
                    from datetime import datetime, timedelta
                    time_obj = datetime.strptime(d.get('time'), "%H:%M")
                    end_time_obj = time_obj + timedelta(hours=1)
                    d['end_time'] = end_time_obj.strftime('%H:%M')
                    print(f"[DEBUG] 本日の終了時間を1時間後に強制設定: {d.get('time')} -> {d['end_time']}")
            # 来週（AIが既に展開済みの場合はスキップ）
            if re.search(r'来週', phrase) and not ai_already_expanded_next_week:
                # 来週の月曜日を計算
                days_until_next_monday = (7 - now.weekday()) % 7
                if days_until_next_monday == 0:  # 今日が月曜日の場合
                    days_until_next_monday = 7
                next_monday = now + timedelta(days=days_until_next_monday)

                # 元のテキストから時刻条件を抽出
                default_start_time = '08:00'
                default_end_time = '22:00'

                # 「X時以降」のパターンをチェック
                time_after_match = re.search(r'(\d{1,2})時以降', original_text)
                if time_after_match:
                    hour = int(time_after_match.group(1))
                    default_start_time = f"{hour:02d}:00"
                    default_end_time = '23:59'
                    print(f"[DEBUG] 来週 + X時以降を検出: {hour}時以降 -> {default_start_time}〜{default_end_time}")

                # 「X時-Y時」のパターンをチェック
                time_range_match = re.search(r'(\d{1,2})[\-〜~](\d{1,2})時', original_text)
                if time_range_match:
                    start_hour = int(time_range_match.group(1))
                    end_hour = int(time_range_match.group(2))
                    default_start_time = f"{start_hour:02d}:00"
                    default_end_time = f"{end_hour:02d}:00"
                    print(f"[DEBUG] 来週 + 時間範囲を検出: {start_hour}-{end_hour}時 -> {default_start_time}〜{default_end_time}")

                # 来週の7日間を生成
                week_dates = []
                for i in range(7):
                    week_date = next_monday + timedelta(days=i)
                    week_dates.append(week_date.strftime('%Y-%m-%d'))

                # 来週の各日付に対して空き時間確認のエントリを作成
                for week_date in week_dates:
                    week_entry = {
                        'date': week_date,
                        'time': default_start_time,
                        'end_time': default_end_time
                    }
                    if not any(existing.get('date') == week_date for existing in new_dates):
                        new_dates.append(week_entry)
                        print(f"[DEBUG] 来週の日付を追加: {week_date} {default_start_time}〜{default_end_time}")

                # 元のエントリは削除（来週の処理で置き換え）
                continue
            # AIが既に来週を展開済みの場合は、時刻だけ補完
            elif re.search(r'来週', phrase) and ai_already_expanded_next_week:
                print(f"[DEBUG] AI既展開の来週エントリ、時刻のみ補完")
                # 「X時以降」のパターンをチェック（end_timeは常に23:59に強制設定）
                time_after_match = re.search(r'(\d{1,2})時以降', original_text)
                if time_after_match:
                    hour = int(time_after_match.group(1))
                    if not d.get('time'):
                        d['time'] = f"{hour:02d}:00"
                    # end_timeは常に23:59に強制設定
                    d['end_time'] = '23:59'
                    print(f"[DEBUG] X時以降を補完（強制）: {hour}時以降 -> {d['time']}〜{d['end_time']}")
            # 来月
            if re.search(r'来月', phrase):
                # 来月の1日を計算
                if now.month == 12:
                    next_month_year = now.year + 1
                    next_month = 1
                else:
                    next_month_year = now.year
                    next_month = now.month + 1
                
                # 来月の全日を生成
                import calendar
                days_in_month = calendar.monthrange(next_month_year, next_month)[1]
                month_dates = []
                for day in range(1, days_in_month + 1):
                    month_date = datetime(next_month_year, next_month, day).strftime('%Y-%m-%d')
                    month_dates.append(month_date)
                
                # 来月の各日付に対して空き時間確認のエントリを作成
                for month_date in month_dates:
                    month_entry = {
                        'date': month_date,
                        'time': '08:00',
                        'end_time': '22:00'
                    }
                    if not any(existing.get('date') == month_date for existing in new_dates):
                        new_dates.append(month_entry)
                        print(f"[DEBUG] 来月の日付を追加: {month_date}")
                
                # 元のエントリは削除（来月の処理で置き換え）
                continue
            # ○月（例：1月、2月など）
            month_match = re.search(r'(\d{1,2})月', phrase)
            if month_match:
                month_num = int(month_match.group(1))
                target_year = now.year
                
                # 過去の月の場合は来年として扱う
                if month_num < now.month:
                    target_year = now.year + 1
                
                # 指定月の全日を生成
                import calendar
                days_in_month = calendar.monthrange(target_year, month_num)[1]
                month_dates = []
                for day in range(1, days_in_month + 1):
                    month_date = datetime(target_year, month_num, day).strftime('%Y-%m-%d')
                    month_dates.append(month_date)
                
                # 指定月の各日付に対して空き時間確認のエントリを作成
                for month_date in month_dates:
                    month_entry = {
                        'date': month_date,
                        'time': '08:00',
                        'end_time': '22:00'
                    }
                    if not any(existing.get('date') == month_date for existing in new_dates):
                        new_dates.append(month_entry)
                        print(f"[DEBUG] {month_num}月の日付を追加: {month_date}")
                
                # 元のエントリは削除（月の処理で置き換え）
                continue
            # 今日から1週間
            if re.search(r'今日から1週間', phrase):
                d['date'] = now.strftime('%Y-%m-%d')
                d['end_date'] = (now + timedelta(days=6)).strftime('%Y-%m-%d')
                d['time'] = '00:00'
                d['end_time'] = '23:59'
            # end_timeが空
            if d.get('time') and not d.get('end_time'):
                # 終了時間が設定されていない場合は1時間後に設定
                from datetime import datetime, timedelta
                time_obj = datetime.strptime(d.get('time'), "%H:%M")
                end_time_obj = time_obj + timedelta(hours=1)
                d['end_time'] = end_time_obj.strftime('%H:%M')
            # title補完
            if not d.get('title') or d['title'] == '':
                if d.get('description'):
                    d['title'] = d['description']
                elif parsed.get('task_type') == 'add_event':
                    t = d.get('time', '')
                    e = d.get('end_time', '')
                    d['title'] = f"予定（{d.get('date', '')} {t}〜{e}）"
            new_dates.append(d)
        print(f"[DEBUG] new_dates(AI+補完): {new_dates}")
        # 2. 正規表現で漏れた枠を「追加」する（AI抽出に無い場合のみ）
        pattern1 = r'(\d{1,2})/(\d{1,2})[\s　]*([0-9]{1,2}):?([0-9]{0,2})[\-〜~]([0-9]{1,2}):?([0-9]{0,2})'
        matches1 = re.findall(pattern1, original_text)
        print(f"[DEBUG] pattern1マッチ: {matches1}")
        for m in matches1:
            month, day, sh, sm, eh, em = m
            year = now.year
            try:
                dt = datetime(year, int(month), int(day))
                if dt < now:
                    dt = datetime(year+1, int(month), int(day))
            except Exception:
                continue
            date_str = dt.strftime('%Y-%m-%d')
            start_time = f"{int(sh):02d}:{sm if sm else '00'}"
            end_time = f"{int(eh):02d}:{em if em else '00'}"
            if not any(d.get('date') == date_str and d.get('time') == start_time and d.get('end_time') == end_time for d in new_dates):
                new_date_entry = {
                    'date': date_str,
                    'time': start_time,
                    'end_time': end_time,
                    'description': ''
                }
                if parsed.get('task_type') == 'add_event':
                    new_date_entry['title'] = f"予定（{date_str} {start_time}〜{end_time}）"
                new_dates.append(new_date_entry)
                print(f"[DEBUG] pattern1で追加: {new_date_entry}")
        pattern2 = r'[・\-]\s*(\d{1,2})/(\d{1,2})\s*([0-9]{1,2})-([0-9]{1,2})時'
        matches2 = re.findall(pattern2, original_text)
        print(f"[DEBUG] pattern2マッチ: {matches2}")
        for m in matches2:
            month, day, sh, eh = m
            year = now.year
            try:
                dt = datetime(year, int(month), int(day))
                if dt < now:
                    dt = datetime(year+1, int(month), int(day))
            except Exception:
                continue
            date_str = dt.strftime('%Y-%m-%d')
            start_time = f"{int(sh):02d}:00"
            end_time = f"{int(eh):02d}:00"
            if not any(d.get('date') == date_str and d.get('time') == start_time and d.get('end_time') == end_time for d in new_dates):
                new_date_entry = {
                    'date': date_str,
                    'time': start_time,
                    'end_time': end_time,
                    'description': ''
                }
                if parsed.get('task_type') == 'add_event':
                    new_date_entry['title'] = f"予定（{date_str} {start_time}〜{end_time}）"
                new_dates.append(new_date_entry)
                print(f"[DEBUG] pattern2で追加: {new_date_entry}")
        pattern3 = r'(\d{1,2})/(\d{1,2})\s*([0-9]{1,2})時?-([0-9]{1,2})時?'
        matches3 = re.findall(pattern3, original_text)
        print(f"[DEBUG] pattern3マッチ: {matches3}")
        for m in matches3:
            month, day, sh, eh = m
            year = now.year
            try:
                dt = datetime(year, int(month), int(day))
                if dt < now:
                    dt = datetime(year+1, int(month), int(day))
            except Exception:
                continue
            date_str = dt.strftime('%Y-%m-%d')
            start_time = f"{int(sh):02d}:00"
            end_time = f"{int(eh):02d}:00"
            if not any(d.get('date') == date_str and d.get('time') == start_time and d.get('end_time') == end_time for d in new_dates):
                new_date_entry = {
                    'date': date_str,
                    'time': start_time,
                    'end_time': end_time,
                    'description': ''
                }
                if parsed.get('task_type') == 'add_event':
                    new_date_entry['title'] = f"予定（{date_str} {start_time}〜{end_time}）"
                new_dates.append(new_date_entry)
                print(f"[DEBUG] pattern3で追加: {new_date_entry}")
        
        # 月が指定されていない場合（例：16日11:30-14:00）の処理
        pattern4 = r'(\d{1,2})日\s*([0-9]{1,2}):?([0-9]{0,2})[\-〜~]([0-9]{1,2}):?([0-9]{0,2})'
        matches4 = re.findall(pattern4, original_text)
        print(f"[DEBUG] pattern4マッチ（日のみ）: {matches4}")
        for m in matches4:
            day, sh, sm, eh, em = m
            year = now.year
            month = now.month
            try:
                dt = datetime(year, month, int(day))
                # 過去の日付の場合は来月として扱う
                if dt < now:
                    if month == 12:
                        dt = datetime(year+1, 1, int(day))
                    else:
                        dt = datetime(year, month+1, int(day))
            except Exception:
                continue
            date_str = dt.strftime('%Y-%m-%d')
            start_time = f"{int(sh):02d}:{sm if sm else '00'}"
            end_time = f"{int(eh):02d}:{em if em else '00'}"
            if not any(d.get('date') == date_str and d.get('time') == start_time and d.get('end_time') == end_time for d in new_dates):
                new_date_entry = {
                    'date': date_str,
                    'time': start_time,
                    'end_time': end_time,
                    'description': ''
                }
                if parsed.get('task_type') == 'add_event':
                    new_date_entry['title'] = f"予定（{date_str} {start_time}〜{end_time}）"
                new_dates.append(new_date_entry)
                print(f"[DEBUG] pattern4で追加（日のみ）: {new_date_entry}")
        
        # 複数の時間帯が同じ日に指定されている場合（例：16日11:30-14:00/15:00-17:00）
        pattern5 = r'(\d{1,2})日\s*([0-9]{1,2}):?([0-9]{0,2})[\-〜~]([0-9]{1,2}):?([0-9]{0,2})/([0-9]{1,2}):?([0-9]{0,2})[\-〜~]([0-9]{1,2}):?([0-9]{0,2})'
        matches5 = re.findall(pattern5, original_text)
        print(f"[DEBUG] pattern5マッチ（日のみ複数時間帯）: {matches5}")
        for m in matches5:
            day, sh1, sm1, eh1, em1, sh2, sm2, eh2, em2 = m
            year = now.year
            month = now.month
            try:
                dt = datetime(year, month, int(day))
                # 過去の日付の場合は来月として扱う
                if dt < now:
                    if month == 12:
                        dt = datetime(year+1, 1, int(day))
                    else:
                        dt = datetime(year, month+1, int(day))
            except Exception:
                continue
            date_str = dt.strftime('%Y-%m-%d')
            
            # 1つ目の時間帯
            start_time1 = f"{int(sh1):02d}:{sm1 if sm1 else '00'}"
            end_time1 = f"{int(eh1):02d}:{em1 if em1 else '00'}"
            if not any(d.get('date') == date_str and d.get('time') == start_time1 and d.get('end_time') == end_time1 for d in new_dates):
                new_date_entry1 = {
                    'date': date_str,
                    'time': start_time1,
                    'end_time': end_time1,
                    'description': ''
                }
                if parsed.get('task_type') == 'add_event':
                    new_date_entry1['title'] = f"予定（{date_str} {start_time1}〜{end_time1}）"
                new_dates.append(new_date_entry1)
                print(f"[DEBUG] pattern5で追加（1つ目）: {new_date_entry1}")
            
            # 2つ目の時間帯
            start_time2 = f"{int(sh2):02d}:{sm2 if sm2 else '00'}"
            end_time2 = f"{int(eh2):02d}:{em2 if em2 else '00'}"
            if not any(d.get('date') == date_str and d.get('time') == start_time2 and d.get('end_time') == end_time2 for d in new_dates):
                new_date_entry2 = {
                    'date': date_str,
                    'time': start_time2,
                    'end_time': end_time2,
                    'description': ''
                }
                if parsed.get('task_type') == 'add_event':
                    new_date_entry2['title'] = f"予定（{date_str} {start_time2}〜{end_time2}）"
                new_dates.append(new_date_entry2)
                print(f"[DEBUG] pattern5で追加（2つ目）: {new_date_entry2}")
        
        # より柔軟な日付解析：改行やスペースで区切られた複数の日付に対応
        # 例：「16日11:30-14:00/15:00-17:00\n17日18:00-19:00\n18日9:00-10:00/16:00-16:30/17:30-18:00」
        lines = original_text.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # 各行から日付を抽出
            day_match = re.search(r'(\d{1,2})日', line)
            if not day_match:
                continue
                
            day = int(day_match.group(1))
            year = now.year
            month = now.month
            
            try:
                dt = datetime(year, month, day)
                # 過去の日付の場合は来月として扱う
                if dt < now:
                    if month == 12:
                        dt = datetime(year+1, 1, day)
                    else:
                        dt = datetime(year, month+1, day)
            except Exception:
                continue
                
            date_str = dt.strftime('%Y-%m-%d')
            
            # 時間帯を抽出（複数の時間帯に対応）
            time_pattern = r'([0-9]{1,2}):?([0-9]{0,2})[\-〜~]([0-9]{1,2}):?([0-9]{0,2})'
            time_matches = re.findall(time_pattern, line)
            
            for time_match in time_matches:
                sh, sm, eh, em = time_match
                start_time = f"{int(sh):02d}:{sm if sm else '00'}"
                end_time = f"{int(eh):02d}:{em if em else '00'}"
                
                if not any(d.get('date') == date_str and d.get('time') == start_time and d.get('end_time') == end_time for d in new_dates):
                    new_date_entry = {
                        'date': date_str,
                        'time': start_time,
                        'end_time': end_time,
                        'description': ''
                    }
                    if parsed.get('task_type') == 'add_event':
                        new_date_entry['title'] = f"予定（{date_str} {start_time}〜{end_time}）"
                    new_dates.append(new_date_entry)
                    print(f"[DEBUG] 柔軟な日付解析で追加: {new_date_entry}")
        
        # 本日/今日の処理を追加（AIが既に予定を作成していない場合のみ）
        if ('本日' in original_text or '今日' in original_text) and not new_dates:
            date_str = now.strftime('%Y-%m-%d')
            
            # 時間の抽出
            time_pattern = r'(本日|今日)(\d{1,2})時'
            time_match = re.search(time_pattern, original_text)
            
            if time_match:
                hour = int(time_match.group(2))
                start_time = f"{hour:02d}:00"
                end_time = f"{hour+1:02d}:00"
                
                # タイトルを抽出
                title_parts = original_text.split()
                title = ""
                for part in title_parts:
                    if part in ['移動', '移動あり', '移動時間', '移動必要']:
                        break
                    if not re.match(r'^\d{1,2}時$', part) and part not in ['本日', '今日']:
                        if title:
                            title += " "
                        title += part
                
                if not title:
                    title = "予定"
                
                print(f"[DEBUG] 抽出されたタイトル: '{title}'")
                
                # メイン予定を作成
                main_event = {
                    'date': date_str,
                    'time': start_time,
                    'end_time': end_time,
                    'title': title,
                    'description': ''
                }
                
                new_dates.append(main_event)
                print(f"[DEBUG] 本日/今日の予定を追加: {main_event}")
        
        print(f"[DEBUG] new_dates(正規表現追加後): {new_dates}")
        
        # 移動時間の自動追加処理（無効化 - line_bot_handler.pyで処理）
        # new_dates = self._add_travel_time(new_dates, original_text)

        parsed['dates'] = new_dates
        return parsed
    
    def _add_travel_time(self, dates, original_text):
        """移動時間を自動追加する処理"""
        from datetime import datetime, timedelta
        import pytz
        
        # 移動キーワードをチェック
        travel_keywords = ['移動', '移動あり', '移動時間', '移動必要']
        has_travel = any(keyword in original_text for keyword in travel_keywords)
        
        print(f"[DEBUG] 移動時間チェック: original_text='{original_text}', has_travel={has_travel}")
        print(f"[DEBUG] 移動キーワード: {travel_keywords}")
        
        if not has_travel:
            print(f"[DEBUG] 移動キーワードが見つからないため、移動時間を追加しません")
            return dates
        
        print(f"[DEBUG] 移動時間の自動追加を開始")
        
        jst = pytz.timezone('Asia/Tokyo')
        new_dates = []
        
        for date_info in dates:
            # 元の予定を追加
            new_dates.append(date_info)
            
            # 移動時間を追加するかチェック
            if self._should_add_travel_time(date_info, original_text):
                travel_events = self._create_travel_events(date_info, jst)
                
                # 移動時間の重複チェック
                for travel_event in travel_events:
                    is_duplicate = False
                    for existing_date in new_dates:
                        if (existing_date.get('date') == travel_event.get('date') and 
                            existing_date.get('time') == travel_event.get('time') and 
                            existing_date.get('end_time') == travel_event.get('end_time')):
                            is_duplicate = True
                            print(f"[DEBUG] 重複する移動時間をスキップ: {travel_event}")
                            break
                    
                    if not is_duplicate:
                        new_dates.append(travel_event)
                        print(f"[DEBUG] 移動時間を追加: {travel_event}")
        
        return new_dates
    
    def _should_add_travel_time(self, date_info, original_text):
        """移動時間を追加すべきかチェック"""
        # 移動キーワードが含まれている場合のみ追加
        travel_keywords = ['移動', '移動あり', '移動時間', '移動必要']
        result = any(keyword in original_text for keyword in travel_keywords)
        print(f"[DEBUG] _should_add_travel_time: original_text='{original_text}', result={result}")
        return result
    
    def _create_travel_events(self, main_event, jst):
        """移動時間の予定を作成"""
        from datetime import datetime, timedelta
        
        print(f"[DEBUG] _create_travel_events開始: main_event={main_event}")
        travel_events = []
        date_str = main_event['date']
        start_time = main_event['time']
        end_time = main_event['end_time']
        
        # 開始時間と終了時間をdatetimeオブジェクトに変換
        start_dt = jst.localize(datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M"))
        end_dt = jst.localize(datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M"))
        
        # 移動前の予定（1時間前）
        travel_before_dt = start_dt - timedelta(hours=1)
        travel_before_end_dt = start_dt
        
        travel_before_event = {
            'date': date_str,
            'time': travel_before_dt.strftime('%H:%M'),
            'end_time': travel_before_end_dt.strftime('%H:%M'),
            'title': '移動時間（往路）',
            'description': '移動のための時間'
        }
        travel_events.append(travel_before_event)
        
        # 移動後の予定（1時間後）
        travel_after_dt = end_dt
        travel_after_end_dt = end_dt + timedelta(hours=1)
        
        travel_after_event = {
            'date': date_str,
            'time': travel_after_dt.strftime('%H:%M'),
            'end_time': travel_after_end_dt.strftime('%H:%M'),
            'title': '移動時間（復路）',
            'description': '移動のための時間'
        }
        travel_events.append(travel_after_event)
        
        print(f"[DEBUG] 作成された移動時間イベント: {travel_events}")
        return travel_events
    
    def extract_event_info(self, text):
        """イベント追加用の情報を抽出します"""
        try:
            now_jst = self._get_jst_now_str()
            system_prompt = (
                f"あなたは予定とタスクを管理するAIです。\n"
                f"現在の日時（日本時間）は {now_jst} です。  \n"
                "この日時は、すべての自然言語の解釈において**常に絶対的な基準**としてください。  \n"
                "会話の流れや前回の入力に引きずられることなく、**毎回この現在日時を最優先にしてください。**\n"
                "\n"
                "あなたはイベント情報抽出の専門家です。ユーザーのテキストからイベントのタイトルと日時を抽出し、以下のJSON形式で返してください。\n\n"
                "抽出ルール:\n"
                "1. イベントのタイトルは、直前の人名や主語、会議名なども含めて、できるだけ長く・具体的に抽出してください。\n"
                "   例:『田中さんとMTG 新作アプリの件』→タイトル:『田中さんとMTG』、説明:『新作アプリの件』\n"
                "2. 開始日時と終了日時を抽出（終了時間が明示されていない場合は1時間後をデフォルトとする）\n"
                "3. 日本語の日付表現を具体的な日付に変換\n"
                "4. 時間表現を24時間形式に変換\n"
                "5. タイムゾーンは日本時間（JST）を想定\n\n"
                "出力形式:\n"
                "{\n  \"title\": \"イベントタイトル\",\n  \"start_datetime\": \"2024-01-15T09:00:00\",\n  \"end_datetime\": \"2024-01-15T10:00:00\",\n  \"description\": \"説明（オプション）\"\n}\n"
            )
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": text
                    }
                ],
                temperature=0.1
            )
            result = response.choices[0].message.content
            parsed = self._parse_ai_response(result)
            # --- タイトルが短すぎる場合は人名や主語＋MTGなどを含めて補完 ---
            if parsed and isinstance(parsed, dict) and 'title' in parsed:
                title = parsed['title']
                # 例: "MTG"や"会議"など短い場合は元テキストから人名＋MTGを抽出
                if title and len(title) <= 4:
                    import re
                    # 例: "田中さんとMTG" "佐藤さん会議" "山田さんMTG" など
                    m = re.search(r'([\w一-龠ぁ-んァ-ン]+さん[と]?\s*MTG|[\w一-龠ぁ-んァ-ン]+さん[と]?\s*会議)', text)
                    if m:
                        parsed['title'] = m.group(1)
            return parsed
        except Exception as e:
            return {"error": f"AI処理エラー: {str(e)}"}
    
    def format_calendar_response(self, events_info):
        """カレンダー情報を読みやすい形式にフォーマットします"""
        if not events_info:
            return "📅 指定された日付に予定はありません。"
        
        response = "📅 カレンダー情報\n\n"
        
        for day_info in events_info:
            if 'error' in day_info:
                response += f"❌ {day_info['date']}: {day_info['error']}\n\n"
                continue
            
            date = day_info['date']
            events = day_info['events']
            
            if not events:
                response += f"📅 {date}: 予定なし（空いています）\n\n"
            else:
                response += f"📅 {date}:\n"
                for event in events:
                    start_time = self._format_datetime(event['start'])
                    end_time = self._format_datetime(event['end'])
                    response += f"  • {event['title']} ({start_time} - {end_time})\n"
                response += "\n"
        
        return response
    
    def _format_datetime(self, datetime_str):
        """日時文字列を読みやすい形式にフォーマットします"""
        try:
            dt = parser.parse(datetime_str)
            return dt.strftime('%m/%d %H:%M')
        except:
            return datetime_str
    
    def format_event_confirmation(self, success, message, event_info):
        """
        イベント追加結果をフォーマットします
        予定が入っている場合：
        ❌予定が入っています！\n\n• タイトル (MM/DD HH:MM - HH:MM)
        予定を追加した場合：
        ✅予定を追加しました！\n\n📅タイトル\nM/D（曜）HH:MM〜HH:MM
        """
        if success:
            response = "✅予定を追加しました！\n\n"
            if event_info:
                title = event_info.get('title', '')
                start = event_info.get('start')
                end = event_info.get('end')
                if start and end:
                    from datetime import datetime
                    import pytz
                    jst = pytz.timezone('Asia/Tokyo')
                    start_dt = datetime.fromisoformat(start).astimezone(jst)
                    end_dt = datetime.fromisoformat(end).astimezone(jst)
                    weekday = "月火水木金土日"[start_dt.weekday()]
                    date_str = f"{start_dt.month}/{start_dt.day}（{weekday}）"
                    time_str = f"{start_dt.strftime('%H:%M')}〜{end_dt.strftime('%H:%M')}"
                    response += f"📅{title}\n{date_str}{time_str}"
        else:
            response = "❌予定が入っています！\n\n"
            if event_info and isinstance(event_info, list):
                for event in event_info:
                    title = event.get('title', '')
                    start = event.get('start')
                    end = event.get('end')
                    if start and end:
                        from datetime import datetime
                        import pytz
                        jst = pytz.timezone('Asia/Tokyo')
                        start_dt = datetime.fromisoformat(start).astimezone(jst)
                        end_dt = datetime.fromisoformat(end).astimezone(jst)
                        date_str = f"{start_dt.month:02d}/{start_dt.day:02d}"
                        time_str = f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"
                        response += f"• {title} ({date_str} {time_str})\n"
        return response
    
    def check_multiple_dates_availability(self, dates_info):
        """複数の日付の空き時間を確認するための情報を抽出します"""
        try:
            now_jst = self._get_jst_now_str()
            system_prompt = (
                f"あなたは予定とタスクを管理するAIです。\n"
                f"現在の日時（日本時間）は {now_jst} です。  \n"
                "この日時は、すべての自然言語の解釈において**常に絶対的な基準**としてください。  \n"
                "会話の流れや前回の入力に引きずられることなく、**毎回この現在日時を最優先にしてください。**\n"
                "\n"
                "複数の日付の空き時間確認リクエストを処理してください。以下のJSON形式で返してください。\n\n"
                "出力形式:\n"
                "{\n  \"dates\": [\n    {\n      \"date\": \"2024-01-15\",\n      \"time_range\": \"09:00-18:00\"\n    }\n  ]\n}\n"
            )
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": dates_info
                    }
                ],
                temperature=0.1
            )
            
            result = response.choices[0].message.content
            return self._parse_ai_response(result)
            
        except Exception as e:
            return {"error": f"AI処理エラー: {str(e)}"}
    
    def format_free_slots_response(self, free_slots_by_date):
        """
        free_slots_by_date: { 'YYYY-MM-DD': [{'start': '10:00', 'end': '11:00'}, ...], ... }
        指定フォーマットで空き時間を返す
        """
        jst = pytz.timezone('Asia/Tokyo')
        if not free_slots_by_date:
            return "✅空き時間はありませんでした。"
        
        # 空き時間がない日を除外
        dates_with_slots = {date: slots for date, slots in free_slots_by_date.items() if len(slots) > 0}
        
        if not dates_with_slots:
            return "✅空き時間はありませんでした。"
        
        response = "✅以下が空き時間です！\n\n"
        for date, slots in dates_with_slots.items():
            dt = jst.localize(datetime.strptime(date, "%Y-%m-%d"))
            weekday = "月火水木金土日"[dt.weekday()]
            response += f"{dt.month}/{dt.day}（{weekday}）\n"
            for slot in slots:
                response += f"・{slot['start']}〜{slot['end']}\n"
        return response
    
    def format_free_slots_response_by_frame(self, free_slots_by_frame):
        """
        free_slots_by_frame: [
            {'date': 'YYYY-MM-DD', 'start_time': 'HH:MM', 'end_time': 'HH:MM', 'free_slots': [{'start': 'HH:MM', 'end': 'HH:MM'}, ...]},
            ...
        ]
        日付ごとに空き時間をまとめて返す（重複枠・重複時間帯は除外）
        """
        logger.info(f"[format_free_slots_response_by_frame] 開始、フレーム数: {len(free_slots_by_frame)}")

        jst = pytz.timezone('Asia/Tokyo')
        if not free_slots_by_frame:
            logger.info(f"[format_free_slots_response_by_frame] 空のデータ")
            return "✅空き時間はありませんでした。"

        # 日付ごとに空き時間をまとめる
        date_slots = {}
        for i, frame in enumerate(free_slots_by_frame):
            date = frame.get('date')
            slots = frame.get('free_slots', [])

            if not date:
                logger.warning(f"[format_free_slots_response_by_frame] フレーム{i+1}: 日付なし")
                continue

            if date not in date_slots:
                date_slots[date] = set()

            # 空き時間を追加
            for slot in slots:
                if slot.get('start') and slot.get('end'):
                    date_slots[date].add((slot['start'], slot['end']))

        logger.info(f"[format_free_slots_response_by_frame] 集計結果: {len(date_slots)}日分")

        # 空き時間がない日を除外
        dates_with_slots = {date: slots for date, slots in date_slots.items() if len(slots) > 0}

        if not dates_with_slots:
            logger.info(f"[format_free_slots_response_by_frame] 空き時間なし")
            return "✅空き時間はありませんでした。"

        # レスポンスを構築
        response_lines = ["✅以下が空き時間です！\n"]

        for date in sorted(dates_with_slots.keys()):
            try:
                dt = jst.localize(datetime.strptime(date, "%Y-%m-%d"))
                weekday = "月火水木金土日"[dt.weekday()]
                response_lines.append(f"{dt.month}/{dt.day}（{weekday}）")

                # 時刻順にソート
                slots = sorted(list(dates_with_slots[date]))
                for start, end in slots:
                    response_lines.append(f"・{start}〜{end}")

                logger.info(f"[format_free_slots_response_by_frame] {date}: {len(slots)}件の空き時間")
            except Exception as e:
                logger.error(f"[format_free_slots_response_by_frame] 日付{date}の処理エラー: {e}")

        response = "\n".join(response_lines)
        logger.info(f"[format_free_slots_response_by_frame] レスポンス構築完了（{len(response_lines)}行）")

        return response 