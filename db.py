import os
import sqlite3
from datetime import datetime, timedelta
import secrets
import string
import logging

logger = logging.getLogger(__name__)

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    PG_BINARY = psycopg2.Binary
except ImportError:
    psycopg2 = None
    PG_BINARY = lambda x: x

DB_PATH = 'line_calendar.db'

class DBHelper:
    def __init__(self, db_path=DB_PATH):
        db_url = os.getenv('DATABASE_URL')
        self.is_postgres = False
        self.db_url = db_url
        self.db_path = db_path
        
        if db_url and psycopg2 is not None:
            self.is_postgres = True
            # 接続プールを作成
            self.connection_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=db_url,
                cursor_factory=psycopg2.extras.DictCursor
            )
            self.conn = self._get_connection()
        else:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
        
        self._init_tables()

    def _get_connection(self):
        """データベース接続を取得（再接続機能付き）"""
        if not self.is_postgres:
            return self.conn
        
        try:
            # 接続プールから接続を取得
            conn = self.connection_pool.getconn()
            # 接続が有効かテスト
            conn.cursor().execute('SELECT 1')
            return conn
        except Exception as e:
            logger.warning(f"データベース接続エラー: {e}")
            # 接続プールを再作成
            try:
                self.connection_pool.closeall()
            except:
                pass
            self.connection_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=self.db_url,
                cursor_factory=psycopg2.extras.DictCursor
            )
            return self.connection_pool.getconn()

    def _execute_with_retry(self, operation):
        """データベース操作をリトライ機能付きで実行"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return operation()
            except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
                if "connection already closed" in str(e) or "connection" in str(e).lower():
                    logger.warning(f"データベース接続エラー (試行 {attempt + 1}/{max_retries}): {e}")
                    if attempt < max_retries - 1:
                        # 接続を再取得
                        if self.is_postgres:
                            try:
                                self.connection_pool.putconn(self.conn)
                            except:
                                pass
                            self.conn = self._get_connection()
                        import time
                        time.sleep(1)
                        continue
                raise e
        return operation()

    def _init_tables(self):
        def operation():
            c = self.conn.cursor()
            if self.is_postgres:
                # PostgreSQL: SERIAL型やIF NOT EXISTSの書き方に注意
                c.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        line_user_id TEXT PRIMARY KEY,
                        google_token BYTEA,
                        created_at TEXT,
                        updated_at TEXT
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS onetimes (
                        code TEXT PRIMARY KEY,
                        line_user_id TEXT,
                        expires_at TEXT,
                        used INTEGER DEFAULT 0,
                        created_at TEXT
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS pending_events (
                        line_user_id TEXT PRIMARY KEY,
                        event_json TEXT,
                        created_at TEXT
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS conversation_history (
                        id SERIAL PRIMARY KEY,
                        line_user_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                ''')
                c.execute('''
                    CREATE INDEX IF NOT EXISTS idx_conversation_user_time
                    ON conversation_history(line_user_id, created_at DESC)
                ''')
            else:
                # SQLite
                c.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        line_user_id TEXT PRIMARY KEY,
                        google_token BLOB,
                        created_at TEXT,
                        updated_at TEXT
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS onetimes (
                        code TEXT PRIMARY KEY,
                        line_user_id TEXT,
                        expires_at TEXT,
                        used INTEGER DEFAULT 0,
                        created_at TEXT
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS pending_events (
                        line_user_id TEXT PRIMARY KEY,
                        event_json TEXT,
                        created_at TEXT
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS conversation_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        line_user_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                ''')
                c.execute('''
                    CREATE INDEX IF NOT EXISTS idx_conversation_user_time
                    ON conversation_history(line_user_id, created_at DESC)
                ''')
            self.conn.commit()
        
        self._execute_with_retry(operation)

    # --- users ---
    def save_google_token(self, line_user_id, google_token_bytes):
        now = datetime.utcnow().isoformat()
        print(f"[DEBUG] save_google_token: line_user_id={line_user_id}, token_length={len(google_token_bytes) if google_token_bytes else 0}, time={now}")
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('''
                INSERT INTO users (line_user_id, google_token, created_at, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (line_user_id) DO UPDATE SET google_token=EXCLUDED.google_token, updated_at=EXCLUDED.updated_at
            ''', (line_user_id, PG_BINARY(google_token_bytes), now, now))
        else:
            c.execute('''
                INSERT INTO users (line_user_id, google_token, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(line_user_id) DO UPDATE SET google_token=excluded.google_token, updated_at=excluded.updated_at
            ''', (line_user_id, google_token_bytes, now, now))
        self.conn.commit()

    def get_google_token(self, line_user_id):
        def operation():
            c = self.conn.cursor()
            if self.is_postgres:
                c.execute('SELECT google_token FROM users WHERE line_user_id=%s', (line_user_id,))
            else:
                c.execute('SELECT google_token FROM users WHERE line_user_id=?', (line_user_id,))
            row = c.fetchone()
            print(f"[DEBUG] get_google_token: line_user_id={line_user_id}, token_found={row is not None}, token_length={len(row[0]) if row and row[0] else 0}")
            return row[0] if row else None
        
        return self._execute_with_retry(operation)

    # --- onetimes ---
    def create_onetime_code(self, line_user_id, code, expires_minutes=10):
        now = datetime.utcnow()
        expires_at = (now + timedelta(minutes=expires_minutes)).isoformat()
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('''
                INSERT INTO onetimes (code, line_user_id, expires_at, used, created_at)
                VALUES (%s, %s, %s, 0, %s)
            ''', (code, line_user_id, expires_at, now.isoformat()))
        else:
            c.execute('''
                INSERT INTO onetimes (code, line_user_id, expires_at, used, created_at)
                VALUES (?, ?, ?, 0, ?)
            ''', (code, line_user_id, expires_at, now.isoformat()))
        self.conn.commit()

    def get_onetime_code(self, code):
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('SELECT code, line_user_id, expires_at, used FROM onetimes WHERE code=%s', (code,))
        else:
            c.execute('SELECT code, line_user_id, expires_at, used FROM onetimes WHERE code=?', (code,))
        row = c.fetchone()
        if row:
            return {
                'code': row[0],
                'line_user_id': row[1],
                'expires_at': row[2],
                'used': bool(row[3])
            }
        return None

    def mark_onetime_code_used(self, code):
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('UPDATE onetimes SET used=1 WHERE code=%s', (code,))
        else:
            c.execute('UPDATE onetimes SET used=1 WHERE code=?', (code,))
        self.conn.commit()

    def generate_onetime_code(self, line_user_id, expires_minutes=10):
        """ワンタイムコードを生成してDBに保存"""
        # 8文字のランダムコードを生成
        code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        expires_at = (datetime.now() + timedelta(minutes=expires_minutes)).isoformat()
        created_at = datetime.now().isoformat()
        
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('''
                INSERT INTO onetimes (code, line_user_id, expires_at, created_at)
                VALUES (%s, %s, %s, %s)
            ''', (code, line_user_id, expires_at, created_at))
        else:
            c.execute('''
                INSERT INTO onetimes (code, line_user_id, expires_at, created_at)
                VALUES (?, ?, ?, ?)
            ''', (code, line_user_id, expires_at, created_at))
        self.conn.commit()
        
        return code

    def verify_onetime_code(self, code):
        """ワンタイムコードを検証（有効期限・使用済みチェック）"""
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('''
                SELECT line_user_id, expires_at, used 
                FROM onetimes 
                WHERE code = %s
            ''', (code,))
        else:
            c.execute('''
                SELECT line_user_id, expires_at, used 
                FROM onetimes 
                WHERE code = ?
            ''', (code,))
        result = c.fetchone()
        
        if not result:
            return None  # コードが存在しない
        
        line_user_id, expires_at, used = result
        
        if used:
            return None  # 既に使用済み
        
        # 有効期限チェック
        expires_datetime = datetime.fromisoformat(expires_at)
        if datetime.now() > expires_datetime:
            return None  # 期限切れ
        
        return line_user_id

    def mark_onetime_used(self, code):
        """ワンタイムコードを使用済みにマーク"""
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('UPDATE onetimes SET used = 1 WHERE code = %s', (code,))
        else:
            c.execute('UPDATE onetimes SET used = 1 WHERE code = ?', (code,))
        self.conn.commit()

    def cleanup_expired_onetimes(self):
        """期限切れのワンタイムコードを削除"""
        c = self.conn.cursor()
        now = datetime.now().isoformat()
        if self.is_postgres:
            c.execute('DELETE FROM onetimes WHERE expires_at < %s', (now,))
        else:
            c.execute('DELETE FROM onetimes WHERE expires_at < ?', (now,))
        self.conn.commit()

    def user_exists(self, line_user_id):
        """ユーザーが認証済みかどうかを判定"""
        def operation():
            c = self.conn.cursor()
            if self.is_postgres:
                c.execute('SELECT 1 FROM users WHERE line_user_id = %s', (line_user_id,))
            else:
                c.execute('SELECT 1 FROM users WHERE line_user_id = ?', (line_user_id,))
            return c.fetchone() is not None
        
        return self._execute_with_retry(operation)

    def get_all_user_ids(self):
        """認証済みユーザーのLINEユーザーID一覧を返す（google_tokenがNULLや空でないユーザーのみ）"""
        def operation():
            c = self.conn.cursor()
            if self.is_postgres:
                c.execute('SELECT line_user_id FROM users WHERE google_token IS NOT NULL AND octet_length(google_token) > 0')
            else:
                c.execute('SELECT line_user_id FROM users WHERE google_token IS NOT NULL AND length(google_token) > 0')
            rows = c.fetchall()
            return [row[0] for row in rows]
        
        return self._execute_with_retry(operation)

    def close(self):
        if self.is_postgres:
            try:
                self.connection_pool.putconn(self.conn)
                self.connection_pool.closeall()
            except:
                pass
        else:
            self.conn.close()

    def save_oauth_state(self, state, line_user_id):
        """OAuth stateとLINEユーザーIDを紐付けて保存"""
        c = self.conn.cursor()
        now = datetime.now().isoformat()
        if self.is_postgres:
            c.execute('''
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    line_user_id TEXT,
                    created_at TEXT
                )
            ''')
            c.execute('''
                INSERT INTO oauth_states (state, line_user_id, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (state) DO UPDATE SET line_user_id=EXCLUDED.line_user_id, created_at=EXCLUDED.created_at
            ''', (state, line_user_id, now))
        else:
            c.execute('''
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    line_user_id TEXT,
                    created_at TEXT
                )
            ''')
            c.execute('''
                INSERT OR REPLACE INTO oauth_states (state, line_user_id, created_at)
                VALUES (?, ?, ?)
            ''', (state, line_user_id, now))
        self.conn.commit()

    def get_line_user_id_by_state(self, state):
        """stateからLINEユーザーIDを取得"""
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('SELECT line_user_id FROM oauth_states WHERE state = %s', (state,))
        else:
            c.execute('SELECT line_user_id FROM oauth_states WHERE state = ?', (state,))
        result = c.fetchone()
        return result[0] if result else None

    def save_pending_event(self, line_user_id, event_json):
        now = datetime.utcnow().isoformat()
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('''
                INSERT INTO pending_events (line_user_id, event_json, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT(line_user_id) DO UPDATE SET event_json=EXCLUDED.event_json, created_at=EXCLUDED.created_at
            ''', (line_user_id, event_json, now))
        else:
            c.execute('''
                INSERT INTO pending_events (line_user_id, event_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(line_user_id) DO UPDATE SET event_json=excluded.event_json, created_at=excluded.created_at
            ''', (line_user_id, event_json, now))
        self.conn.commit()

    def get_pending_event(self, line_user_id):
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('SELECT event_json FROM pending_events WHERE line_user_id=%s', (line_user_id,))
        else:
            c.execute('SELECT event_json FROM pending_events WHERE line_user_id=?', (line_user_id,))
        row = c.fetchone()
        return row[0] if row else None

    def delete_pending_event(self, line_user_id):
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('DELETE FROM pending_events WHERE line_user_id=%s', (line_user_id,))
        else:
            c.execute('DELETE FROM pending_events WHERE line_user_id=?', (line_user_id,))
        self.conn.commit()

    def save_conversation_message(self, line_user_id, role, content):
        """会話メッセージを保存（role: 'user' or 'assistant'）"""
        now = datetime.utcnow().isoformat()
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('''
                INSERT INTO conversation_history (line_user_id, role, content, created_at)
                VALUES (%s, %s, %s, %s)
            ''', (line_user_id, role, content, now))
        else:
            c.execute('''
                INSERT INTO conversation_history (line_user_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
            ''', (line_user_id, role, content, now))
        self.conn.commit()

    def get_conversation_history(self, line_user_id, limit=10):
        """直近N件の会話履歴を取得"""
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('''
                SELECT role, content, created_at
                FROM conversation_history
                WHERE line_user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            ''', (line_user_id, limit))
        else:
            c.execute('''
                SELECT role, content, created_at
                FROM conversation_history
                WHERE line_user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            ''', (line_user_id, limit))
        rows = c.fetchall()
        # 時系列順に並び替え（古い順）
        return [{'role': row[0], 'content': row[1], 'created_at': row[2]} for row in reversed(rows)]

    def clear_old_conversation_history(self, line_user_id, keep_count=20):
        """古い会話履歴を削除（最新N件のみ保持）"""
        c = self.conn.cursor()
        if self.is_postgres:
            c.execute('''
                DELETE FROM conversation_history
                WHERE line_user_id = %s
                AND id NOT IN (
                    SELECT id FROM conversation_history
                    WHERE line_user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                )
            ''', (line_user_id, line_user_id, keep_count))
        else:
            c.execute('''
                DELETE FROM conversation_history
                WHERE line_user_id = ?
                AND id NOT IN (
                    SELECT id FROM conversation_history
                    WHERE line_user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                )
            ''', (line_user_id, line_user_id, keep_count))
        self.conn.commit()