# test_lab — a local test lab for StarCraft II bots

A Django web app for running automated SC2 bot matches and tracking the results.
It wraps the [AI Arena local-play-bootstrap](https://github.com/aiarena/local-play-bootstrap)
infrastructure in Docker, so your bot plays in the same environment it uses on
the [AI Arena ladder](https://aiarena.net) — but locally, under your control.

**What you get:**

- **Four match types** — vs Blizzard AI, vs custom bots (any aiarena-supported
  type: Python, C++, Java, …), vs past versions of your own bot (git A/B), and
  replay-continuation tests
- **Test suites** — bundle matchups into suites (default: the 15 Blizzard AI
  variants — 3 races × 5 builds) and run them with one click
- **Tickets** — agent prompts for AI-assisted development, each ticket working
  in its own git worktree, with tests triggered on commit
- **Results, logs & replays** — every match records its result, full bot logs,
  and replay download
- **API** — `POST /api/trigger-tests/` to fire matches or suites from scripts,
  git hooks, or CI

Every match, suite run, and test group is keyed to the bot under test, so
different bots keep completely separate experiment histories.

See [ROADMAP.md](ROADMAP.md) for where this is heading: regression verdicts,
GitHub-native CI, and continuous deployment for bots.

> This is intended to work for all bot types but some types are untested.
> It's all very WIP so feel free to send a PR — I won't be too picky about merging it.

## Requirements

- **Docker** (running)
- **Python 3.12+**
- **StarCraft II maps** on the host (a directory of `.SC2Map` files)

## Quickstart (standalone setup)

Use this if you've cloned test_lab on its own and want to get it running
quickly. The only prerequisites are **Docker** (running), **Python 3.12+**,
and **StarCraft II** installed (for map files).

> **Why not fully Dockerize Django too?** The match runner launches SC2
> Docker containers from the Django process using `docker compose` with
> host-path volume mounts. Running Django itself inside Docker would
> require Docker-in-Docker with complex host/container path mapping that
> is fragile across OSes. Keeping Django on the host avoids this entirely.

### Automated setup (recommended)

Clone, run one command, and the browser opens automatically:

```bash
mkdir sc2_test_lab && cd sc2_test_lab
git clone <test_lab_repo_url> test_lab
python test_lab/quickstart/setup.py
```

The script handles everything: starts MySQL in Docker, creates a virtual
environment, installs dependencies, runs migrations, and launches the
development server. It works on Windows, Linux, and macOS.

> On Debian/Ubuntu you may need `sudo apt install python3-venv` first.

### Manual setup (step by step)

<details>
<summary>Click to expand manual steps</summary>
#### 1. Clone into the right directory structure

Django needs `test_lab` to be an importable Python package, so clone it
**inside** a wrapper directory:

```bash
mkdir sc2_test_lab && cd sc2_test_lab
git clone git@github.com:chadspratt/sc_bot_test_lab.git test_lab
```

Your layout should look like:

```
sc2_test_lab/          # <-- you'll run commands from here
  test_lab/            # <-- the cloned repo
    quickstart/
    aiarena/
    models.py
    ...
```

#### 2. Start MySQL via Docker

```bash
docker compose -f test_lab/quickstart/docker-compose.yml up -d
```

This starts a MySQL 8.0 container on `localhost:3306` with database `sc_bot`
(user `root`, password `testlab`). Adjust credentials in the compose file or
via environment variables — see `quickstart/settings.py` for the full list.

#### 3. Create a Python virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux / macOS
source venv/bin/activate

pip install -r test_lab/requirements.txt
```

#### 4. Run database migrations

```bash
python test_lab/quickstart/manage.py migrate --database default
python test_lab/quickstart/manage.py migrate test_lab --database sc_bot_test_lab
```

#### 5. Start the development server

```bash
python test_lab/quickstart/manage.py runserver
```

Open <http://localhost:8000/test_lab/> in your browser.

### Stopping / resetting

```bash
# Stop MySQL (data is preserved in a Docker volume)
docker compose -f test_lab/quickstart/docker-compose.yml down

# Stop MySQL and delete all data
docker compose -f test_lab/quickstart/docker-compose.yml down -v
```

</details>

#### Reverse-proxy / remote access (waitress + CSRF env vars)

Django's built-in dev server cannot parse chunked transfer-encoding, which
many reverse proxies (e.g. `tailscale serve`) use for POST requests — form
submissions behind such a proxy will fail with `403 (CSRF token missing)`.
For remote access, run a real WSGI server instead and opt in to forwarded
headers:

```bash
pip install waitress

# Example: expose at https://your-host.ts.net:8443/test_lab/ via `tailscale serve`
TEST_LAB_CSRF_TRUSTED_ORIGINS="https://your-host.ts.net:8443" \
TEST_LAB_USE_FORWARDED=1 \
python -m waitress --listen=0.0.0.0:8000 test_lab.quickstart.wsgi:application
```

- `TEST_LAB_CSRF_TRUSTED_ORIGINS` — comma-separated list of proxy origins
  trusted for CSRF (scheme + host + port must match exactly).
- `TEST_LAB_USE_FORWARDED=1` — honor `X-Forwarded-Host` / `X-Forwarded-Proto`.

Both default to unset, so local `runserver` setups are unaffected.

## Adding to an existing Django project

If you're adding test_lab to an existing Django project instead of using
the quickstart, follow these steps.

### Database

test_lab uses its own MySQL database (the router in `quickstart/db_router.py`
sends all test_lab models to the `sc_bot_test_lab` alias — define it in your
project's settings). Run migrations with:

```bash
python manage.py migrate test_lab --database sc_bot_test_lab
```

## Using the app

### 6. Basic Configuration

In the app, go to `Config > System` and enter the path for the directory containing the maps.
Also enter the path to SC2Switcher.exe to enable launching replays from the app.

### 7. Register bots

Go to `Config > Custom Bots` and follow the instructions for adding a bot.

A bot marked as a **test subject** can act as Player 1 in matches. 
**Source Path** defaults to the aiarena package but you can change this to point to your live development code

Symlinks/junctions in the source directory are auto-detected and stored so
Docker volume mounts resolve correctly.

On `Run Match > Vs Blizzard AI` you can trigger a test run of the bot vs the Blizzard AI. The first run may take a while to build docker images

### 8. Matches
There are 4 kinds of matches that can be run. the `Run Match` page allows running an individual match
* Blizzard AI - custom bot vs in-game bot
* Custom Bot - custom bot vs custom bot
* Past Version - custom bot vs itself. can be use a past version or the current version for a true mirror match
* Replay - custom bot vs in-game bot, but you start from a game state that is pulled from a replay. Will require some edits for a custom bot to make good use of this, since the misleading clock and lack of game state will probably trip up all but the simplest bots.

### 9. Test Suites
`Config > Test Suites` allows you to bundle different matchups in to a suite that can be run. There is a default **Blizzard AI** suite for running vs 15 variants of the Blizzard AI (3 races * 5 builds). Test Suites can be attached to Tickets to be run automatically

### 10. Tickets
`Tickets` is a system for generating agent prompts that can be run in the editor of your choice.
The prompt instructs the agent to work in a git worktree so that multiple tickets can be worked on concurrently.
After finishing the work, the agent is instructed to commit and trigger the ticket tests, which will run against the code in the worktree

`Config > Prompt Templates` Allows for creating custom templates for working on specific bots. There are forms for creating and editing them but they are stored in actual files so it's probably easier to edit manage them with a normal text editor. In the app you can register them to specific bots. When creating a ticket you can choose from registered templates for the bot or, if there are none, the default prompt.

---
### Git Commit Hook
To automatically trigger a test suite on every commit, add a `post-commit`
hook to the bot's git repo:

```bash
#!/bin/sh
# Post-commit hook: trigger test_lab test suite via the Django API
# Replace TEST_BOT_ID with the bot's numeric ID from the Custom Bots page.

BRANCH=$(git rev-parse --abbrev-ref HEAD)
SHORT_SHA=$(git rev-parse --short HEAD)
COMMIT_MSG=$(git log -1 --format=%s)
DESCRIPTION="$BRANCH $SHORT_SHA: $COMMIT_MSG"

curl -s -X POST http://localhost:8000/test_lab/api/trigger-tests/ \
  -H "Content-Type: application/json" \
  -d "{\"description\": \"$DESCRIPTION\", \"difficulty\": \"CheatInsane\", \"test_bot_id\": TEST_BOT_ID}" \
  > /dev/null 2>&1 &

echo "Test suite triggered for commit $SHORT_SHA"
```

Save this as `.git/hooks/post-commit` and make it executable (`chmod +x`).

## API

### `POST /test_lab/api/trigger-tests/`

JSON body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `test_bot_id` | int | **required** | Custom bot ID for Player 1 (the test subject) |
| `difficulty` | string | `"CheatInsane"` | AI difficulty level |
| `description` | string | `""` | Test group description |
| `custom_bot_id` | int | *null* | When set, runs a single match vs this bot instead of the full test suite |
| `test_suite_id` | int | *null* | Run a specific test suite (falls back to the bot's default suite, then "Blizzard AI") |
| `branch` | string | `""` | Git branch name — creates a worktree so the bot source is mounted from that branch |
