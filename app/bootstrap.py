import os
import time
from uuid import uuid4

from app import auth
from app.storage import postgres_connection, mongo_client, minio_client


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

        # Day 3 additive status migration, in the same PostgreSQL transaction.
        # Keeps all rows, files, IDs and existing statuses.
        connection.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_status_check")
        connection.execute("""
            ALTER TABLE documents ADD CONSTRAINT documents_status_check
            CHECK (status IN ('pending', 'ready', 'failed', 'deleting'))
        """)

        # Day 6: admin-managed accounts. `role` is a plain CHECK constraint,
        # additive like the migration above, so a future role only needs the
        # same kind of DROP/ADD here.
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


def initialize_mongodb():
    with mongo_client() as client:
        collection = client[os.environ["MONGO_DB"]]["document_details"]
        collection.create_index("document_id", unique=True)


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
            "SELECT id, username, role, is_active, created_at FROM users LIMIT 0"
        )


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


def seed_admin_user():
    """Best-effort, idempotent: create the first admin from env vars if none
    exists yet. Never overwrites an existing account with the same username.
    Skipped (not fatal) when ADMIN_USERNAME/ADMIN_PASSWORD are unset, so
    existing .env files without them still boot.
    """
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
            "INSERT INTO users (id, username, password_hash, role) VALUES (%s, %s, %s, 'admin')",
            (uuid4(), username, auth.hash_password(password)),
        )
    print(f"Admin seed: created admin '{username}'", flush=True)


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
    initialize_with_retry(
        "PostgreSQL", initialize_postgres, check_postgres
    )
    seed_admin_user()
    initialize_with_retry(
        "MongoDB", initialize_mongodb, check_mongodb
    )
    initialize_with_retry(
        "MinIO", initialize_minio, check_minio
    )
    print("Bootstrap: PASS")
