# Insighta CLI

Install globally:

```bash
pip install .
```

Then use:

```bash
insighta login --api-url http://localhost:8000
insighta whoami
insighta profiles list --gender male
insighta profiles search "young males from nigeria"
insighta profiles create --name "Mary Jane"
insighta profiles export --format csv
insighta profiles upload --file .\profiles.csv
```

Credentials are stored at `~/.insighta/credentials.json`.
