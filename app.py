import os
import logging
logging.basicConfig(level=logging.INFO)
# Railway環境でcredentials.jsonを書き出す
if "GOOGLE_CREDENTIALS_FILE" in os.environ:
    with open("credentials.json", "w") as f:
        f.write(os.environ["GOOGLE_CREDENTIALS_FILE"])

from flask import Flask, request, abort, render_template_string, redirect, url_for, session, Response, make_response
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from line_bot_handler import LineBotHandler
from config import Config
import json
from datetime import datetime
import pickle
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from db import DBHelper
from werkzeug.middleware.proxy_fix import ProxyFix
from ai_service import AIService
from send_daily_agenda import send_daily_agenda

# ログ設定
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')

# ProxyFixを追加
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# 設定の検証
try:
    Config.validate_config()
    logger.info("設定の検証が完了しました")
except ValueError as e:
    logger.error(f"設定エラー: {e}")
    raise

# LINEボットハンドラーを初期化
try:
    line_bot_handler = LineBotHandler()
    handler = line_bot_handler.get_handler()
    logger.info("LINEボットハンドラーの初期化が完了しました")
except Exception as e:
    logger.error(f"LINEボットハンドラーの初期化に失敗しました: {e}")
    raise

print("DEBUG: OPENAI_API_KEY =", os.getenv("OPENAI_API_KEY"))

# DBヘルパーの初期化
db_helper = DBHelper()

@app.route("/callback", methods=['POST'])
def callback():
    """LINE Webhookのコールバックエンドポイント"""
    # リクエストヘッダーからX-Line-Signatureを取得
    signature = request.headers['X-Line-Signature']

    # リクエストボディを取得
    body = request.get_data(as_text=True)
    logger.info("Request body: " + body)

    try:
        # 署名を検証し、問題なければhandleに定義されている関数を呼び出す
        handler.handle(body, signature)
    except InvalidSignatureError:
        # 署名検証で失敗したときは例外をあげる
        logger.error("署名検証に失敗しました")
        abort(400)

    # 正常終了時は200を返す
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """テキストメッセージを処理"""
    try:
        logger.info(f"メッセージを受信: {event.message.text}")
        
        # メッセージを処理してレスポンスを取得
        response = line_bot_handler.handle_message(event)
        
        # LINEにメッセージを送信
        line_bot_handler.line_bot_api.reply_message(
            event.reply_token,
            response
        )
        
        logger.info("メッセージの処理が完了しました")
        
    except Exception as e:
        logger.error(f"メッセージ処理でエラーが発生しました: {e}")
        # エラーが発生した場合はエラーメッセージを送信
        try:
            line_bot_handler.line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="申し訳ございません。エラーが発生しました。しばらく時間をおいて再度お試しください。")
            )
        except Exception as reply_error:
            logger.error(f"エラーメッセージの送信に失敗しました: {reply_error}")

@app.route("/", methods=['GET'])
def index():
    """ヘルスチェック用エンドポイント"""
    return "LINE Calendar Bot is running!"

@app.route("/health", methods=['GET'])
def health():
    """ヘルスチェック用エンドポイント"""
    return {"status": "healthy", "service": "line-calendar-bot"}

@app.route("/test", methods=['GET'])
def test():
    """テスト用エンドポイント"""
    return {
        "message": "LINE Calendar Bot Test",
        "config": {
            "line_configured": bool(Config.LINE_CHANNEL_ACCESS_TOKEN and Config.LINE_CHANNEL_SECRET),
            "openai_configured": bool(Config.OPENAI_API_KEY),
            "google_configured": bool(os.path.exists(Config.GOOGLE_CREDENTIALS_FILE))
        }
    }

@app.route('/onetime_login', methods=['GET', 'POST'])
def onetime_login():
    """ワンタイムコード認証ページ"""
    if request.method == 'GET':
        # ワンタイムコード入力フォームを表示
        html = '''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Google Calendar 認証</title>
            <meta charset="utf-8">
            <style>
                body { font-family: Arial, sans-serif; max-width: 500px; margin: 50px auto; padding: 20px; }
                .form-group { margin-bottom: 20px; }
                label { display: block; margin-bottom: 5px; font-weight: bold; }
                input[type="text"] { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; }
                button { background: #4285f4; color: white; padding: 12px 24px; border: none; border-radius: 4px; cursor: pointer; }
                button:hover { background: #3367d6; }
                .error { color: red; margin-top: 10px; }
                .success { color: green; margin-top: 10px; }
            </style>
        </head>
        <body>
            <h1>Google Calendar 認証</h1>
            <p>LINE BotでGoogle Calendarを利用するために認証が必要です。</p>
            <form method="POST">
                <div class="form-group">
                    <label for="code">ワンタイムコード:</label>
                    <input type="text" id="code" name="code" placeholder="8文字のコードを入力" required>
                </div>
                <button type="submit">認証を開始</button>
            </form>
            {% if error %}
            <div class="error">{{ error }}</div>
            {% endif %}
            {% if success %}
            <div class="success">{{ success }}</div>
            {% endif %}
        </body>
        </html>
        '''
        return render_template_string(html, error=None, success=None)
    
    elif request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        
        # ワンタイムコードを検証
        line_user_id = db_helper.verify_onetime_code(code)
        if not line_user_id:
            html = '''
            <!DOCTYPE html>
            <html>
            <head>
                <title>認証エラー</title>
                <meta charset="utf-8">
                <style>
                    body { font-family: Arial, sans-serif; max-width: 500px; margin: 50px auto; padding: 20px; }
                    .error { color: red; margin: 20px 0; }
                    .back-link { margin-top: 20px; }
                </style>
            </head>
            <body>
                <h1>認証エラー</h1>
                <div class="error">
                    無効なワンタイムコードです。<br>
                    コードが正しいか、有効期限が切れていないか確認してください。
                </div>
                <div class="back-link">
                    <a href="/onetime_login">戻る</a>
                </div>
            </body>
            </html>
            '''
            return render_template_string(html)
        
        # ワンタイムコードを使用済みにマーク
        db_helper.mark_onetime_used(code)
        
        try:
            # Google OAuth認証フローを開始
            SCOPES = ['https://www.googleapis.com/auth/calendar']
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            # --- redirect_uri自動判定 ---
            # Railway本番・ngrok・ローカル全てでhttpsを強制
            base_url = request.url_root.rstrip('/')
            if base_url.startswith('http://'):
                base_url = 'https://' + base_url[len('http://'):]
            flow.redirect_uri = base_url + '/oauth2callback'
            # --- ここまで ---
            auth_url, state = flow.authorization_url(
                access_type='offline',
                include_granted_scopes='true',
                prompt='consent'
            )
            # stateとline_user_idをDBに保存
            db_helper.save_oauth_state(state, line_user_id)
            return redirect(auth_url)
        except Exception as e:
            logging.error(f"Google OAuth認証エラー: {e}")
            html = '''
            <!DOCTYPE html>
            <html>
            <head>
                <title>認証エラー</title>
                <meta charset="utf-8">
                <style>
                    body { font-family: Arial, sans-serif; max-width: 500px; margin: 50px auto; padding: 20px; }
                    .error { color: red; margin: 20px 0; }
                </style>
            </head>
            <body>
                <h1>認証エラー</h1>
                <div class="error">
                    Google認証の初期化に失敗しました。<br>
                    しばらく時間をおいて再度お試しください。
                </div>
            </body>
            </html>
            '''
            return render_template_string(html)

@app.route('/oauth2callback')
def oauth2callback():
    """Google OAuth認証コールバック"""
    from flask import make_response
    try:
        # stateからline_user_idを取得
        state = request.args.get('state')
        line_user_id = db_helper.get_line_user_id_by_state(state)
        if not line_user_id:
            return make_response("認証セッションが無効です", 400)
        # 新たにflowを生成
        SCOPES = ['https://www.googleapis.com/auth/calendar']
        flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
        # --- redirect_uri自動判定 ---
        base_url = request.url_root.rstrip('/')
        if base_url.startswith('http://'):
            base_url = 'https://' + base_url[len('http://'):]
        flow.redirect_uri = base_url + '/oauth2callback'
        # --- ここまで ---
        # 認証コードを取得してトークンを交換（スコープ警告を無視）
        import warnings
        import oauthlib.oauth2.rfc6749.parameters
        # スコープ検証を無効化
        original_validate_token_parameters = oauthlib.oauth2.rfc6749.parameters.validate_token_parameters
        def dummy_validate_token_parameters(params):
            return True
        oauthlib.oauth2.rfc6749.parameters.validate_token_parameters = dummy_validate_token_parameters
        
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                flow.fetch_token(authorization_response=request.url)
        finally:
            # 元の関数を復元
            oauthlib.oauth2.rfc6749.parameters.validate_token_parameters = original_validate_token_parameters
            
        credentials = flow.credentials
        # トークンをDBに保存
        token_data = pickle.dumps(credentials)
        db_helper.save_google_token(line_user_id, token_data)
        # ワンタイムコードを使用済みに
        db_helper.mark_onetime_code_used(line_user_id)
        # 認証完了画面
        html = "<h2>Google認証が完了しました。LINEに戻って操作を続けてください。</h2>"
        return make_response(html, 200)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return make_response(f"OAuth2コールバックエラー: {e}", 400)

@app.route('/debug/ai_test', methods=['GET', 'POST'])
def debug_ai_test():
    """AI抽出機能のデバッグ用エンドポイント"""
    from flask import render_template_string, request, jsonify
    
    if request.method == 'POST':
        try:
            text = request.form.get('text', '')
            if not text:
                return jsonify({"error": "テキストが入力されていません"})
            
            # AIサービスでテスト
            ai_service = AIService()
            result = ai_service.extract_dates_and_times(text)
            
            return jsonify({
                "input": text,
                "result": result,
                "success": True
            })
            
        except Exception as e:
            return jsonify({
                "error": str(e),
                "success": False
            })
    
    # GETリクエストの場合はテストフォームを表示
    test_form = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>AI抽出テスト</title>
        <meta charset="utf-8">
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            .container { max-width: 600px; margin: 0 auto; }
            textarea { width: 100%; height: 100px; padding: 10px; margin: 10px 0; }
            button { background: #007bff; color: white; padding: 10px 20px; border: none; cursor: pointer; }
            .result { background: #f8f9fa; padding: 15px; margin: 10px 0; border-radius: 5px; }
            pre { white-space: pre-wrap; word-wrap: break-word; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>AI抽出機能テスト</h2>
            <form id="testForm">
                <label for="text">テストテキスト:</label><br>
                <textarea id="text" name="text" placeholder="例: ・7/10 9-10時&#10;・7/11 9-10時"></textarea><br>
                <button type="submit">テスト実行</button>
            </form>
            <div id="result" class="result" style="display: none;">
                <h3>結果:</h3>
                <pre id="resultContent"></pre>
            </div>
        </div>
        
        <script>
        document.getElementById('testForm').addEventListener('submit', function(e) {
            e.preventDefault();
            
            const text = document.getElementById('text').value;
            const resultDiv = document.getElementById('result');
            const resultContent = document.getElementById('resultContent');
            
            resultContent.textContent = '処理中...';
            resultDiv.style.display = 'block';
            
            fetch('/debug/ai_test', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                },
                body: 'text=' + encodeURIComponent(text)
            })
            .then(response => response.json())
            .then(data => {
                resultContent.textContent = JSON.stringify(data, null, 2);
            })
            .catch(error => {
                resultContent.textContent = 'エラー: ' + error;
            });
        });
        </script>
    </body>
    </html>
    """
    return render_template_string(test_form)

@app.route('/api/send_daily_agenda', methods=['POST'])
def api_send_daily_agenda():
    import os
    from flask import request, jsonify
    secret_token = os.environ.get('DAILY_AGENDA_SECRET_TOKEN')
    req_token = request.args.get('token')
    if not secret_token or req_token != secret_token:
        return jsonify({'status': 'error', 'message': 'Invalid or missing token'}), 403
    try:
        send_daily_agenda()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/debug_users', methods=['GET'])
def api_debug_users():
    import os
    from flask import request, jsonify
    secret_token = os.environ.get('DAILY_AGENDA_SECRET_TOKEN')
    req_token = request.args.get('token')
    if not secret_token or req_token != secret_token:
        return jsonify({'status': 'error', 'message': 'Invalid or missing token'}), 403
    from db import DBHelper
    db = DBHelper()
    c = db.conn.cursor()
    c.execute('SELECT line_user_id, LENGTH(google_token), created_at, updated_at FROM users')
    rows = c.fetchall()
    return jsonify({'users': rows})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("LINE Calendar Bot を起動しています...")
    app.run(debug=True, host='0.0.0.0', port=port) 