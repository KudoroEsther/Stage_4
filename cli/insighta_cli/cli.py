import json
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import typer
from rich.console import Console
from rich.status import Status
from rich.table import Table

from insighta_cli.client import InsightaClient
from insighta_cli.config import DEFAULT_API_URL
from insighta_cli.security import derive_code_challenge, generate_code_verifier, generate_state


app = typer.Typer(help="Insighta Labs+ CLI")
profiles_app = typer.Typer(help="Profile commands")
app.add_typer(profiles_app, name="profiles")
console = Console()


class OAuthCallbackServer(HTTPServer):
    def __init__(self, server_address):
        super().__init__(server_address, OAuthCallbackHandler)
        self.code = None
        self.state = None
        self.event = threading.Event()


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        self.server.code = query.get("code", [None])[0]
        self.server.state = query.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>Insighta login complete</h1><p>You can close this window.</p></body></html>"
        )
        self.server.event.set()

    def log_message(self, format, *args):
        return


def choose_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def render_profiles(data: list[dict]) -> None:
    table = Table(show_header=True, header_style="bold cyan")
    for column in ["id", "name", "gender", "age", "age_group", "country_id", "country_name"]:
        table.add_column(column)
    for row in data:
        table.add_row(
            row["id"],
            row["name"],
            row["gender"],
            str(row["age"]),
            row["age_group"],
            row["country_id"],
            row["country_name"],
        )
    console.print(table)


def render_json(data: dict) -> None:
    console.print_json(json.dumps(data))


def handle_response(response: httpx.Response) -> dict:
    payload = response.json()
    if response.status_code >= 400:
        raise typer.BadParameter(payload.get("message", "Request failed"))
    return payload


@app.command()
def login(api_url: str = typer.Option(DEFAULT_API_URL, "--api-url", help="Backend base URL")):
    state = generate_state()
    verifier = generate_code_verifier()
    challenge = derive_code_challenge(verifier)
    port = choose_free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    server = OAuthCallbackServer(("127.0.0.1", port))
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    params = urlencode(
        {
            "client": "cli",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
        }
    )
    auth_url = f"{api_url}/auth/github?{params}"
    webbrowser.open(auth_url)
    console.print("Waiting for GitHub OAuth callback...")
    if not server.event.wait(timeout=180):
        raise typer.BadParameter("Timed out waiting for OAuth callback.")
    if server.state != state:
        raise typer.BadParameter("OAuth state mismatch.")
    if not server.code:
        raise typer.BadParameter("Missing OAuth code.")

    with Status("Exchanging GitHub code for Insighta session...", console=console):
        response = httpx.post(
            f"{api_url}/auth/github/callback",
            json={
                "code": server.code,
                "state": state,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
            timeout=30,
        )
    payload = handle_response(response)
    client = InsightaClient(api_url=api_url)
    client.save_session(payload)
    console.print(f"Logged in as @{payload['user']['username']}")


@app.command()
def logout():
    client = InsightaClient()
    client.logout()
    console.print("Logged out.")


@app.command()
def whoami():
    client = InsightaClient()
    with Status("Fetching account...", console=console):
        response = client.request("GET", "/auth/me")
    payload = handle_response(response)
    render_json(payload["data"])


@profiles_app.command("list")
def list_profiles(
    gender: str | None = None,
    country: str | None = typer.Option(None, "--country"),
    age_group: str | None = typer.Option(None, "--age-group"),
    min_age: int | None = typer.Option(None, "--min-age"),
    max_age: int | None = typer.Option(None, "--max-age"),
    sort_by: str | None = typer.Option(None, "--sort-by"),
    order: str = typer.Option("asc", "--order"),
    page: int = typer.Option(1, "--page"),
    limit: int = typer.Option(10, "--limit"),
):
    client = InsightaClient()
    params = {
        "gender": gender,
        "country_id": country,
        "age_group": age_group,
        "min_age": min_age,
        "max_age": max_age,
        "sort_by": sort_by,
        "order": order,
        "page": page,
        "limit": limit,
    }
    params = {key: value for key, value in params.items() if value is not None}
    with Status("Loading profiles...", console=console):
        response = client.request("GET", "/api/profiles", params=params)
    payload = handle_response(response)
    render_profiles(payload["data"])
    console.print(f"Page {payload['page']} of {payload['total_pages']} | Total {payload['total']}")


@profiles_app.command("get")
def get_profile(profile_id: str):
    client = InsightaClient()
    with Status("Loading profile...", console=console):
        response = client.request("GET", f"/api/profiles/{profile_id}")
    payload = handle_response(response)
    render_json(payload["data"])


@profiles_app.command("search")
def search_profiles(
    query: str,
    page: int = typer.Option(1, "--page"),
    limit: int = typer.Option(10, "--limit"),
):
    client = InsightaClient()
    with Status("Searching profiles...", console=console):
        response = client.request(
            "GET",
            "/api/profiles/search",
            params={"q": query, "page": page, "limit": limit},
        )
    payload = handle_response(response)
    render_profiles(payload["data"])
    console.print(f"Page {payload['page']} of {payload['total_pages']} | Total {payload['total']}")


@profiles_app.command("create")
def create_profile(name: str = typer.Option(..., "--name")):
    client = InsightaClient()
    with Status("Creating profile...", console=console):
        response = client.request("POST", "/api/profiles", json={"name": name})
    payload = handle_response(response)
    render_json(payload["data"])


@profiles_app.command("export")
def export_profiles(
    format: str = typer.Option("csv", "--format"),
    gender: str | None = None,
    country: str | None = typer.Option(None, "--country"),
    age_group: str | None = typer.Option(None, "--age-group"),
    min_age: int | None = typer.Option(None, "--min-age"),
    max_age: int | None = typer.Option(None, "--max-age"),
    sort_by: str | None = typer.Option(None, "--sort-by"),
    order: str = typer.Option("asc", "--order"),
):
    client = InsightaClient()
    params = {
        "format": format,
        "gender": gender,
        "country_id": country,
        "age_group": age_group,
        "min_age": min_age,
        "max_age": max_age,
        "sort_by": sort_by,
        "order": order,
    }
    params = {key: value for key, value in params.items() if value is not None}
    output_path = Path.cwd() / "profiles_export.csv"
    with Status("Exporting profiles...", console=console):
        client.download_file("/api/profiles/export", output_path, params=params)
    console.print(f"Saved CSV to {output_path}")


@profiles_app.command("upload")
def upload_profiles(file: Path = typer.Option(..., "--file", exists=True, file_okay=True, dir_okay=False, readable=True)):
    if file.suffix.lower() != ".csv":
        raise typer.BadParameter("Only .csv files are supported.")

    client = InsightaClient()
    with Status("Uploading CSV profiles...", console=console):
        response = client.upload_file("/api/profiles/upload", file)
    payload = handle_response(response)
    render_json(payload)



def main():
    app()
