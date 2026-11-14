"""Apply Alembic migrations to DATABASE_URL (also upgrades unversioned v0.1 databases)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import settings
from backend.app.migrate import migrate


def main() -> None:
    action = migrate()
    print(f"Database {settings.database_url.split('@')[-1]}: {action} to head.")


if __name__ == "__main__":
    main()
