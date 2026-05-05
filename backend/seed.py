import asyncio
import json
import sys
from pathlib import Path

from app.db import database, engine, metadata
from app.models import profiles
from app.security import new_uuid, utcnow


DEFAULT_SEED_FILE = "seed_data.json"


async def seed(seed_path: str):
    metadata.create_all(engine)
    await database.connect()

    with open(seed_path, encoding="utf-8") as file:
        records = json.load(file)["profiles"]

    existing = {
        row["name"]
        for row in await database.fetch_all(
            profiles.select().with_only_columns(profiles.c.name)
        )
    }

    inserted = 0
    skipped = 0

    for record in records:
        name = record["name"].strip().lower()
        if name in existing:
            skipped += 1
            continue

        await database.execute(
            profiles.insert().values(
                id=new_uuid(),
                name=name,
                gender=record["gender"],
                gender_probability=record["gender_probability"],
                sample_size=0,
                age=record["age"],
                age_group=record["age_group"],
                country_id=record["country_id"],
                country_name=record.get("country_name", ""),
                country_probability=record["country_probability"],
                created_at=utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
        inserted += 1

    await database.disconnect()
    print(f"Inserted={inserted} Skipped={skipped}")


if __name__ == "__main__":
    path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SEED_FILE)
    asyncio.run(seed(str(path)))
