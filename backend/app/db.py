from databases import Database
from sqlalchemy import MetaData, create_engine, text

from app.config import get_settings


settings = get_settings()
database_url = settings.database_url

if database_url.startswith("postgresql://"):
    async_database_url = database_url.replace("postgresql://", "postgresql+asyncpg://")
    sync_database_url = database_url.replace(
        "postgresql://",
        "postgresql+psycopg2://",
    )
else:
    async_database_url = database_url.replace("sqlite:///", "sqlite+aiosqlite:///")
    sync_database_url = database_url


database = Database(async_database_url)
metadata = MetaData()
engine = create_engine(
    sync_database_url,
    connect_args={"check_same_thread": False} if sync_database_url.startswith("sqlite") else {},
)



def prepare_database() -> None:
    """
    Create tables and add the read-heavy indexes we rely on for query latency.

    Execute explicit CREATE INDEX statements so existing local databases also
    receive the new indexes without requiring a separate migration tool.
    """

    metadata.create_all(engine)

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_profiles_gender ON profiles (gender)",
        "CREATE INDEX IF NOT EXISTS ix_profiles_age_group ON profiles (age_group)",
        "CREATE INDEX IF NOT EXISTS ix_profiles_country_id ON profiles (country_id)",
        "CREATE INDEX IF NOT EXISTS ix_profiles_age ON profiles (age)",
        "CREATE INDEX IF NOT EXISTS ix_profiles_gender_probability ON profiles (gender_probability)",
        "CREATE INDEX IF NOT EXISTS ix_profiles_country_probability ON profiles (country_probability)",
        "CREATE INDEX IF NOT EXISTS ix_profiles_created_at ON profiles (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_users_role ON users (role)",
        "CREATE INDEX IF NOT EXISTS ix_refresh_tokens_user_id ON refresh_tokens (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_refresh_tokens_expires_at ON refresh_tokens (expires_at)",
        "CREATE INDEX IF NOT EXISTS ix_oauth_states_expires_at ON oauth_states (expires_at)",
    )

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
