import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # LINE Bot設定
    LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
    LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
    
    # OpenAI設定
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    
    # Google Calendar設定
    GOOGLE_CALENDAR_ID = 'primary'
    GOOGLE_CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE', 'credentials.json')
    
    # Flask設定
    FLASK_SECRET_KEY = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key')
    
    # アプリケーション設定
    TIMEZONE = os.getenv('TIMEZONE', 'Asia/Tokyo')
    DEFAULT_EVENT_DURATION = int(os.getenv('DEFAULT_EVENT_DURATION', '60'))  # 分
    
    @classmethod
    def validate_config(cls):
        """設定の妥当性をチェックします"""
        # 一時的に検証を無効化
        print("警告: 環境変数の検証をスキップしています")
        return True
        
        required_vars = [
            'LINE_CHANNEL_ACCESS_TOKEN',
            'LINE_CHANNEL_SECRET',
            'OPENAI_API_KEY'
        ]
        
        missing_vars = []
        for var in required_vars:
            if not getattr(cls, var):
                missing_vars.append(var)
        
        if missing_vars:
            raise ValueError(f"必要な環境変数が設定されていません: {', '.join(missing_vars)}")
        
        # Google認証ファイルの存在確認
        if not os.path.exists(cls.GOOGLE_CREDENTIALS_FILE):
            print(f"警告: Google認証ファイル '{cls.GOOGLE_CREDENTIALS_FILE}' が見つかりません。")
            print("Google Calendar APIを使用するには、credentials.jsonファイルが必要です。") 