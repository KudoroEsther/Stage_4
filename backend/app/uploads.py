import asyncio
import csv
import io
from collections import Counter
from datetime import UTC, datetime

import sqlalchemy
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import database, engine
from app.models import profiles
from app.security import new_uuid
from app.services import COUNTRY_NAMES, get_age_group


settings = get_settings()
upload_slots = asyncio.Semaphore(settings.max_concurrent_uploads)

REQUIRED_COLUMNS = (
    "name",
    "gender",
    "age",
    "gender_probability",
    "country_id",
    "country_probability",
)
OPTIONAL_COLUMNS = ("country_name", "sample_size")
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
VALID_GENDERS = {"male", "female"}


class CsvLineReader:
    """
    Line-based chunk reader that never loads the whole file into memory.

    Reading chunks in a threadpool keeps large uploads from monopolizing the
    event loop while still letting us process files incrementally.
    """

    def __init__(self, file_obj):
        self._stream = io.TextIOWrapper(
            file_obj,
            encoding="utf-8",
            errors="replace",
            newline="",
        )
        self.header: list[str] | None = None
        self.expected_columns: int | None = None

    def read_chunk(self, chunk_size: int) -> tuple[list[dict], bool]:
        rows: list[dict] = []

        if self.header is None:
            while True:
                header_line = self._stream.readline()
                if not header_line:
                    return [], True
                if header_line.strip():
                    self.header = [
                        column.strip().lower().lstrip("\ufeff")
                        for column in next(csv.reader([header_line]))
                    ]
                    self.expected_columns = len(self.header)
                    break

        while len(rows) < chunk_size:
            raw_line = self._stream.readline()
            if not raw_line:
                return rows, True
            if not raw_line.strip():
                continue

            rows.append(
                {
                    "raw_line": raw_line,
                    "header": self.header,
                    "expected_columns": self.expected_columns,
                }
            )

        return rows, False


def _parse_chunk(raw_rows: list[dict]) -> list[dict]:
    parsed_rows: list[dict] = []

    for raw_row in raw_rows:
        raw_line = raw_row["raw_line"]
        header = raw_row["header"]
        expected_columns = raw_row["expected_columns"]

        if "\ufffd" in raw_line:
            parsed_rows.append({"reason": "malformed_row"})
            continue

        try:
            values = next(csv.reader([raw_line]))
        except csv.Error:
            parsed_rows.append({"reason": "malformed_row"})
            continue

        if len(values) != expected_columns:
            parsed_rows.append({"reason": "malformed_row"})
            continue

        parsed_rows.append(
            {
                "data": {
                    column: value.strip()
                    for column, value in zip(header, values)
                }
            }
        )

    return parsed_rows


def _parse_probability(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0 or parsed > 1:
        return None
    return parsed


def _parse_non_negative_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def validate_csv_row(row: dict) -> tuple[dict | None, str | None]:
    # We normalize upload data to match the existing profile idempotency rule.
    name = row.get("name", "").strip().lower()
    gender = row.get("gender", "").strip().lower()
    country_id = row.get("country_id", "").strip().upper()

    if any(not row.get(column, "").strip() for column in REQUIRED_COLUMNS):
        return None, "missing_fields"
    if not name:
        return None, "missing_fields"
    if gender not in VALID_GENDERS:
        return None, "invalid_gender"

    age = _parse_non_negative_int(row.get("age", ""))
    if age is None:
        return None, "invalid_age"

    gender_probability = _parse_probability(row.get("gender_probability", ""))
    country_probability = _parse_probability(row.get("country_probability", ""))
    if gender_probability is None or country_probability is None:
        return None, "invalid_probability"

    if country_id not in COUNTRY_NAMES:
        return None, "invalid_country_id"

    sample_size = _parse_non_negative_int(row.get("sample_size", "").strip() or "0")
    if sample_size is None:
        return None, "invalid_sample_size"

    return (
        {
            "id": new_uuid(),
            "name": name,
            "gender": gender,
            "gender_probability": gender_probability,
            "sample_size": sample_size,
            "age": age,
            "age_group": get_age_group(age),
            "country_id": country_id,
            "country_name": row.get("country_name", "").strip() or COUNTRY_NAMES[country_id],
            "country_probability": country_probability,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        None,
    )


def _build_insert_statement(rows: list[dict]):
    if engine.dialect.name == "sqlite":
        return sqlite_insert(profiles).values(rows).on_conflict_do_nothing(
            index_elements=[profiles.c.name]
        )
    if engine.dialect.name == "postgresql":
        return postgresql_insert(profiles).values(rows).on_conflict_do_nothing(
            index_elements=[profiles.c.name]
        )
    return profiles.insert().values(rows)


def _insert_batch_sync(rows: list[dict]) -> int:
    if not rows:
        return 0

    statement = _build_insert_statement(rows)
    with engine.begin() as connection:
        result = connection.execute(statement)
        return int(result.rowcount or 0)


async def insert_profile_batch(rows: list[dict]) -> int:
    return await run_in_threadpool(_insert_batch_sync, rows)


async def existing_profile_names(names: list[str]) -> set[str]:
    if not names:
        return set()

    rows = await database.fetch_all(
        sqlalchemy.select(profiles.c.name).where(profiles.c.name.in_(names))
    )
    return {row["name"] for row in rows}


async def process_csv_upload(upload_file) -> dict:
    reader = CsvLineReader(upload_file.file)
    summary = {
        "status": "success",
        "total_rows": 0,
        "inserted": 0,
        "skipped": 0,
        "reasons": Counter(),
    }

    async with upload_slots:
        end_of_file = False

        while not end_of_file:
            raw_chunk, end_of_file = await run_in_threadpool(
                reader.read_chunk,
                settings.csv_upload_chunk_size,
            )
            if not raw_chunk:
                continue

            parsed_chunk = await run_in_threadpool(_parse_chunk, raw_chunk)
            summary["total_rows"] += len(parsed_chunk)

            candidate_rows: list[dict] = []
            seen_in_chunk: set[str] = set()

            for parsed_row in parsed_chunk:
                reason = parsed_row.get("reason")
                if reason:
                    summary["skipped"] += 1
                    summary["reasons"][reason] += 1
                    continue

                row, validation_error = validate_csv_row(parsed_row["data"])
                if validation_error:
                    summary["skipped"] += 1
                    summary["reasons"][validation_error] += 1
                    continue

                if row["name"] in seen_in_chunk:
                    summary["skipped"] += 1
                    summary["reasons"]["duplicate_name"] += 1
                    continue

                seen_in_chunk.add(row["name"])
                candidate_rows.append(row)

            existing_names = await existing_profile_names(
                [row["name"] for row in candidate_rows]
            )

            insertable_rows: list[dict] = []
            for row in candidate_rows:
                if row["name"] in existing_names:
                    summary["skipped"] += 1
                    summary["reasons"]["duplicate_name"] += 1
                    continue
                insertable_rows.append(row)

            inserted = await insert_profile_batch(insertable_rows)
            summary["inserted"] += inserted

            # Rows ignored because of concurrent inserts are still counted as
            # duplicates so uploads can run at the same time safely.
            if inserted < len(insertable_rows):
                duplicates_from_race = len(insertable_rows) - inserted
                summary["skipped"] += duplicates_from_race
                summary["reasons"]["duplicate_name"] += duplicates_from_race

            # Yield between chunks so read traffic keeps getting CPU time.
            await asyncio.sleep(0)

    summary["reasons"] = dict(summary["reasons"])
    return summary
