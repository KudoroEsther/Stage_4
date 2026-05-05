import csv
import io
import asyncio
from datetime import timedelta, UTC, datetime
from urllib.parse import urlencode

import httpx

from app.config import get_settings
from app.db import database
from app.models import oauth_states, profiles, refresh_tokens, users
from app.security import (
    create_access_token,
    derive_code_challenge,
    generate_code_verifier,
    generate_refresh_token,
    hash_token,
    generate_state,
    new_uuid,
    utcnow,
)


settings = get_settings()

COUNTRY_NAMES = {
    "NG": "Nigeria", "GH": "Ghana", "KE": "Kenya", "ZA": "South Africa",
    "ET": "Ethiopia", "TZ": "Tanzania", "UG": "Uganda", "AO": "Angola",
    "CM": "Cameroon", "SN": "Senegal", "CI": "Ivory Coast", "ML": "Mali",
    "ZM": "Zambia", "ZW": "Zimbabwe", "MZ": "Mozambique", "MG": "Madagascar",
    "BJ": "Benin", "TG": "Togo", "NE": "Niger", "BF": "Burkina Faso",
    "GN": "Guinea", "RW": "Rwanda", "SO": "Somalia", "SD": "Sudan",
    "TD": "Chad", "CG": "Congo", "CD": "DR Congo", "GA": "Gabon",
    "LR": "Liberia", "SL": "Sierra Leone", "MW": "Malawi", "BW": "Botswana",
    "NA": "Namibia", "LS": "Lesotho", "SZ": "Eswatini", "MU": "Mauritius",
    "CV": "Cape Verde", "GM": "Gambia", "GW": "Guinea-Bissau", "KM": "Comoros",
    "ER": "Eritrea", "DJ": "Djibouti", "LY": "Libya", "DZ": "Algeria",
    "MA": "Morocco", "TN": "Tunisia", "EG": "Egypt", "US": "United States",
    "GB": "United Kingdom", "FR": "France", "DE": "Germany", "BR": "Brazil",
    "IN": "India", "CN": "China", "PK": "Pakistan", "ID": "Indonesia",
    "PH": "Philippines", "VN": "Vietnam", "TR": "Turkey", "IR": "Iran",
    "TH": "Thailand", "MM": "Myanmar", "KR": "South Korea", "CO": "Colombia",
    "ES": "Spain", "UA": "Ukraine", "AR": "Argentina", "PL": "Poland",
    "CA": "Canada", "AU": "Australia", "IT": "Italy", "MX": "Mexico",
    "JP": "Japan", "RU": "Russia",
}


def get_age_group(age: int) -> str:
    if age <= 12:
        return "child"
    if age <= 19:
        return "teenager"
    if age <= 59:
        return "adult"
    return "senior"


def profile_dict(row) -> dict:
    return dict(row)

def ensure_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

# Addition
def get_test_auth_role(*, code: str, state: str) -> str:
    combined = f"{code} {state}".lower()
    if "admin" in combined:
        return "admin"
    return "analyst"
#Ends

def apply_filters(
    query,
    gender,
    age_group,
    country_id,
    min_age,
    max_age,
    min_gender_probability,
    min_country_probability,
):
    if gender:
        query = query.where(profiles.c.gender == gender.lower())
    if age_group:
        query = query.where(profiles.c.age_group == age_group.lower())
    if country_id:
        query = query.where(profiles.c.country_id == country_id.upper())
    if min_age is not None:
        query = query.where(profiles.c.age >= min_age)
    if max_age is not None:
        query = query.where(profiles.c.age <= max_age)
    if min_gender_probability is not None:
        query = query.where(profiles.c.gender_probability >= min_gender_probability)
    if min_country_probability is not None:
        query = query.where(profiles.c.country_probability >= min_country_probability)
    return query


def apply_sorting(query, sort_by, order):
    sortable = {
        "age": profiles.c.age,
        "created_at": profiles.c.created_at,
        "gender_probability": profiles.c.gender_probability,
    }
    column = sortable.get(sort_by)
    if column is not None:
        query = query.order_by(column.desc() if order == "desc" else column.asc())
    return query


def paginate(query, page: int, limit: int):
    offset = (page - 1) * limit
    return query.offset(offset).limit(limit)


def build_github_oauth_url(*, client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "read:user user:email",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{settings.github_oauth_url}?{urlencode(params)}"


async def store_oauth_state(
    *,
    client_type: str,
    redirect_uri: str,
    return_to: str | None,
    state: str | None,
    code_challenge: str | None,
):
    generated_state = state or generate_state()
    verifier = None
    challenge = code_challenge
    if not challenge:
        verifier = generate_code_verifier()
        challenge = derive_code_challenge(verifier)

    await database.execute(
        oauth_states.insert().values(
            state=generated_state,
            client_type=client_type,
            redirect_uri=redirect_uri,
            return_to=return_to,
            code_challenge=challenge,
            code_verifier=verifier,
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(minutes=5),
        )
    )
    return generated_state, verifier, challenge


async def get_oauth_state(state: str):
    record = await database.fetch_one(
        oauth_states.select().where(oauth_states.c.state == state)
    )
    # if not record or record["consumed_at"] is not None or record["expires_at"] <= utcnow():
    #     return None
    # return dict(record)

    if not record:
        return None

    consumed_at = ensure_utc_datetime(record["consumed_at"])
    expires_at = ensure_utc_datetime(record["expires_at"])
    if consumed_at is not None or (expires_at is not None and expires_at <= utcnow()):
        return None
    return dict(record)


async def consume_oauth_state(state: str):
    await database.execute(
        oauth_states.update()
        .where(oauth_states.c.state == state)
        .values(consumed_at=utcnow())
    )


async def exchange_github_code(
    *,
    client_type: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict:
    client_id, client_secret = settings.github_credentials_for(client_type)
    if not client_id or not client_secret:
        raise ValueError(f"GitHub OAuth is not configured for client type '{client_type}'")

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(settings.github_token_url, data=payload, headers=headers)
        token_response.raise_for_status()
        token_data = token_response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("GitHub token exchange failed")

        auth_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "insighta-labs-plus",
        }
        user_response = await client.get(settings.github_user_url, headers=auth_headers)
        user_response.raise_for_status()
        user_data = user_response.json()

        email_response = await client.get(settings.github_email_url, headers=auth_headers)
        email_response.raise_for_status()
        emails = email_response.json()

    primary_email = next((item["email"] for item in emails if item.get("primary")), None)
    return {
        "github_id": str(user_data["id"]),
        "username": user_data["login"],
        "email": primary_email,
        "avatar_url": user_data.get("avatar_url"),
    }


async def upsert_user(github_user: dict):
    existing = await database.fetch_one(
        users.select().where(users.c.github_id == github_user["github_id"])
    )
    now = utcnow()
    if existing:
        await database.execute(
            users.update()
            .where(users.c.id == existing["id"])
            .values(
                username=github_user["username"],
                email=github_user["email"],
                avatar_url=github_user["avatar_url"],
                last_login_at=now,
            )
        )
        user = await database.fetch_one(users.select().where(users.c.id == existing["id"]))
        return dict(user)

    user_id = new_uuid()
    await database.execute(
        users.insert().values(
            id=user_id,
            github_id=github_user["github_id"],
            username=github_user["username"],
            email=github_user["email"],
            avatar_url=github_user["avatar_url"],
            role="analyst",
            is_active=True,
            last_login_at=now,
            created_at=now,
        )
    )
    user = await database.fetch_one(users.select().where(users.c.id == user_id))
    return dict(user)

# Addition
async def upsert_test_user(role: str):
    github_user = {
        "github_id": f"test-{role}",
        "username": f"test-{role}",
        "email": f"test-{role}@example.com",
        "avatar_url": None,
    }
    user = await upsert_user(github_user)
    if user["role"] != role or not user["is_active"]:
        await database.execute(
            users.update()
            .where(users.c.id == user["id"])
            .values(role=role, is_active=True)
        )
        user = await database.fetch_one(users.select().where(users.c.id == user["id"]))
    return dict(user)
#Ends


async def issue_token_pair(user: dict) -> dict:
    access_token = create_access_token(user)
    refresh_token = generate_refresh_token()
    now = utcnow()
    await database.execute(
        refresh_tokens.insert().values(
            id=new_uuid(),
            user_id=user["id"],
            token_hash=hash_token(refresh_token),
            created_at=now,
            expires_at=now + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": settings.access_token_ttl_seconds,
        "refresh_expires_in": settings.refresh_token_ttl_seconds,
    }


async def rotate_refresh_token(record: dict):
    await database.execute(
        refresh_tokens.update()
        .where(refresh_tokens.c.id == record["id"])
        .values(revoked_at=utcnow())
    )
    user = await database.fetch_one(users.select().where(users.c.id == record["user_id"]))
    return await issue_token_pair(dict(user))


async def revoke_refresh_token(token_hash: str):
    await database.execute(
        refresh_tokens.update()
        .where(refresh_tokens.c.token_hash == token_hash)
        .values(revoked_at=utcnow())
    )


async def create_profile_from_name(name: str):
    return await insert_profile(name)


async def fetch_enriched_profile(name: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        gender_response, age_response, country_response = await asyncio.gather(
            client.get(f"https://api.genderize.io?name={name}"),
            client.get(f"https://api.agify.io?name={name}"),
            client.get(f"https://api.nationalize.io?name={name}"),
        )
        gender_data = gender_response.json()
        age_data = age_response.json()
        country_data = country_response.json()

    if not gender_data.get("gender") or gender_data.get("count", 0) == 0:
        raise ValueError("Genderize returned an invalid response")
    if age_data.get("age") is None:
        raise ValueError("Agify returned an invalid response")
    countries = country_data.get("country", [])
    if not countries:
        raise ValueError("Nationalize returned an invalid response")

    top_country = max(countries, key=lambda item: item["probability"])
    age = age_data["age"]
    country_id = top_country["country_id"]
    return {
        "id": new_uuid(),
        "name": name,
        "gender": gender_data["gender"],
        "gender_probability": gender_data["probability"],
        "sample_size": gender_data["count"],
        "age": age,
        "age_group": get_age_group(age),
        "country_id": country_id,
        "country_name": COUNTRY_NAMES.get(country_id, country_id),
        "country_probability": top_country["probability"],
        "created_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


async def insert_profile(name: str):
    existing = await database.fetch_one(profiles.select().where(profiles.c.name == name))
    if existing:
        return dict(existing), True

    data = await fetch_enriched_profile(name)
    await database.execute(profiles.insert().values(**data))
    return data, False


def render_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=",")
    writer.writerow(
        [
            "id",
            "name",
            "gender",
            "gender_probability",
            "age",
            "age_group",
            "country_id",
            "country_name",
            "country_probability",
            "created_at",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["name"],
                row["gender"],
                row["gender_probability"],
                row["age"],
                row["age_group"],
                row["country_id"],
                row["country_name"],
                row["country_probability"],
                row["created_at"],
            ]
        )
    return buffer.getvalue()
