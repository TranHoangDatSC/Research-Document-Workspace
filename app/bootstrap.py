"""Run before uvicorn (Dockerfile CMD): creates tables, indexes and the
bucket if missing (idempotent), seeds the first admin, and refuses to start
in production with placeholder secrets. The check_* functions are reused by
/health/ready. There is no migration tool: schema changes are additive
statements below."""
import os
import time
from uuid import uuid4

from app import auth
from app.storage import postgres_connection, mongo_client, minio_client, redis_client


def bucket_name():
    return os.environ["MINIO_BUCKET"]


def initialize_postgres():
    with postgres_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id UUID PRIMARY KEY,
                name VARCHAR(200) NOT NULL CHECK (length(trim(name)) > 0),
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id UUID PRIMARY KEY,
                project_id UUID NOT NULL REFERENCES projects(id),
                original_name TEXT NOT NULL,
                object_name TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL,
                size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
                status VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'ready', 'failed')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS documents_project_id_idx
            ON documents(project_id)
        """)

        # Adds the 'deleting' status (used by retryable deletes).
        connection.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_status_check")
        connection.execute("""
            ALTER TABLE documents ADD CONSTRAINT documents_status_check
            CHECK (status IN ('pending', 'ready', 'failed', 'deleting'))
        """)

        # Accounts. A new role only needs the CHECK constraint widened.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY,
                username VARCHAR(50) NOT NULL UNIQUE CHECK (length(trim(username)) > 0),
                password_hash TEXT NOT NULL,
                role VARCHAR(20) NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
                is_active BOOLEAN NOT NULL DEFAULT true,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Email: optional, unique ignoring case.
        connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(254)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_key ON users (lower(email))")
        # One-time tokens (reset / verify / key_access). Only the SHA-256 is
        # stored, so a database leak gives no working links.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token_hash CHAR(64) PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TIMESTAMPTZ NOT NULL,
                used_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Session revocation: cookies carry this counter; bumping it ends them.
        connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0")
        # Email verification: existing rows become verified (DEFAULT true),
        # new accounts start unverified (SET DEFAULT false).
        connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT true")
        connection.execute("ALTER TABLE users ALTER COLUMN email_verified SET DEFAULT false")
        connection.execute("ALTER TABLE password_reset_tokens ADD COLUMN IF NOT EXISTS purpose VARCHAR(10) NOT NULL DEFAULT 'reset'")
        # Settings admins change at runtime (e.g. whether sign-up is open).
        connection.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key VARCHAR(50) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Project owner. Nullable for old rows; assign_unowned_projects()
        # gives them to the first admin.
        connection.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES users(id)")
        connection.execute("CREATE INDEX IF NOT EXISTS projects_owner_id_idx ON projects(owner_id)")


def initialize_mongodb():
    with mongo_client() as client:
        collection = client[os.environ["MONGO_DB"]]["document_details"]
        collection.create_index("document_id", unique=True)
        # Chat history is always read per (project, user), newest first.
        client[os.environ["MONGO_DB"]]["chat_messages"].create_index(
            [("project_id", 1), ("user_id", 1), ("created_at", -1), ("seq", -1)]
        )


def initialize_minio():
    client = minio_client()
    if not client.bucket_exists(bucket_name()):
        client.make_bucket(bucket_name())


def check_postgres():
    with postgres_connection() as connection:
        connection.execute(
            "SELECT id, name, description, created_at FROM projects LIMIT 0"
        )
        connection.execute("""
            SELECT id, project_id, original_name, object_name,
                   content_type, size_bytes, status, created_at
            FROM documents LIMIT 0
        """)
        connection.execute(
            "SELECT id, username, email, email_verified, role, is_active, session_version, created_at FROM users LIMIT 0"
        )
        connection.execute("SELECT key, value FROM app_settings LIMIT 0")
        connection.execute("SELECT owner_id FROM projects LIMIT 0")
        connection.execute("SELECT token_hash, user_id, expires_at, used_at, purpose FROM password_reset_tokens LIMIT 0")


def check_mongodb():
    with mongo_client() as client:
        client.admin.command("ping")
        indexes = client[os.environ["MONGO_DB"]][
            "document_details"
        ].index_information()
        valid = any(
            index.get("unique") is True
            and index.get("key") == [("document_id", 1)]
            for index in indexes.values()
        )
        if not valid:
            raise RuntimeError("Missing unique document_id index")


def check_minio():
    if not minio_client().bucket_exists(bucket_name()):
        raise RuntimeError("Missing document bucket")


def check_redis():
    """"up", or "disabled" when REDIS_URL is unset; raises when unreachable."""
    client = redis_client()
    if client is None:
        return "disabled"
    client.ping()
    return "up"


def seed_admin_user():
    """Creates the admin from ADMIN_USERNAME/ADMIN_PASSWORD if that username
    doesn't exist. Never overwrites; skipped when the variables are unset."""
    username = os.environ.get("ADMIN_USERNAME")
    password = os.environ.get("ADMIN_PASSWORD")
    if not username or not password:
        print("Admin seed: skipped (ADMIN_USERNAME/ADMIN_PASSWORD not set)", flush=True)
        return
    with postgres_connection() as connection:
        exists = connection.execute(
            "SELECT 1 FROM users WHERE username = %s", (username,)
        ).fetchone()
        if exists:
            print(f"Admin seed: '{username}' already exists, skipped", flush=True)
            return
        connection.execute(
            "INSERT INTO users (id, username, password_hash, role, email_verified) VALUES (%s, %s, %s, 'admin', true)",
            (uuid4(), username, auth.hash_password(password)),
        )
    print(f"Admin seed: created admin '{username}'", flush=True)


def assign_unowned_projects():
    """Gives ownerless projects to the oldest admin. Idempotent."""
    with postgres_connection() as connection:
        admin = connection.execute(
            "SELECT id FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if admin is None:
            print("Project owners: no admin yet, unowned projects stay hidden", flush=True)
            return
        moved = connection.execute(
            "UPDATE projects SET owner_id = %s WHERE owner_id IS NULL", (admin[0],)
        ).rowcount
    if moved:
        print(f"Project owners: {moved} existing project(s) assigned to the first admin", flush=True)


SECRET_SETTINGS = (
    "SESSION_SECRET", "ADMIN_PASSWORD", "POSTGRES_PASSWORD", "MONGO_INITDB_ROOT_PASSWORD", "MINIO_ROOT_PASSWORD",
    "REDIS_PASSWORD",
)


def check_secrets():
    """Placeholder or short secrets: a warning locally, a refusal to start
    in production (APP_BASE_URL is https)."""
    problems = [name for name in SECRET_SETTINGS if "REPLACE_WITH" in os.environ.get(name, "")]
    if len(os.environ.get("SESSION_SECRET", "")) < 32:
        problems.append("SESSION_SECRET (< 32 ký tự)")
    if not problems:
        return
    message = "Secrets: chưa đổi giá trị mẫu / quá yếu: " + ", ".join(problems)
    if os.environ.get("APP_BASE_URL", "").startswith("https://"):
        raise SystemExit(message + " — từ chối khởi động trên production")
    print("WARNING " + message + " (chấp nhận được khi chạy local)", flush=True)


def initialize_with_retry(name, initialize, check):
    for attempt in range(1, 6):
        try:
            initialize()
            check()
            print(f"{name}: initialized and verified", flush=True)
            return
        except Exception as exc:
            print(
                f"{name}: attempt {attempt}/5 failed "
                f"({type(exc).__name__})",
                flush=True,
            )
            if attempt == 5:
                raise SystemExit(f"{name}: initialization failed")
            time.sleep(2)


if __name__ == "__main__":
    check_secrets()
    initialize_with_retry(
        "PostgreSQL", initialize_postgres, check_postgres
    )
    seed_admin_user()
    assign_unowned_projects()
    initialize_with_retry(
        "MongoDB", initialize_mongodb, check_mongodb
    )
    initialize_with_retry(
        "MinIO", initialize_minio, check_minio
    )
    print("Bootstrap: PASS")
