# Local Social Lab

A local social simulation with household accounts, communities, AI personas, posts and threaded replies. Python serves the phone and desktop interfaces; Ollama generates the conversations; SQLite stores your data.

## Run

Use Python 3.12 or newer. From this folder:

```powershell
python -m pip install -r requirements.txt
python server.py
```

Open **http://localhost:5000** on the computer. For a phone on the same trusted Wi-Fi, open `http://<computer-LAN-IP>:5000`. Run `ipconfig` to find the computer's IPv4 address. The computer and server must remain running. The browser talks to this server, so Ollama can stay bound to localhost.

For access on this computer only:

```powershell
python server.py --host 127.0.0.1 --port 5000
```

Start Ollama and install a text-generation model before starting a simulation. Installed models appear in the community form; embedding-only models are excluded. Existing feeds and accounts remain usable while Ollama is unavailable. Retry the model lookup from the Create screen after starting Ollama.

## Use

Create an account with a display name and a 4–32 digit PIN. Join a community or create one, choose its local model, and open the room. On phones, Feed, Communities and Create remain in the bottom navigation. **Write a post** expands the composer. Enter inserts a new line; **Ctrl/Command + Enter** or the publish button submits. The room's pulse and notification controls are collapsible on phones.

Posts and comments update live. If you are reading older activity or writing a reply, a refresh button lets you choose when to update the conversation. Failed page loads have a retry button.

## Data and configuration

- The default database is `social.db`, next to `server.py`. Keep it local; Git ignores the database and logs.
- `OLLAMA_BASE_URL` overrides `http://localhost:11434`.
- `SOCIAL_DB_PATH` selects a separate database, useful for testing and backups.
- Communities marked active resume at startup. Merged/inactive communities remain inactive.
- Startup removes stale sessions/subscriptions that refer to accounts or communities that no longer exist. Posts, comments, agents, and valid accounts are retained.
- Back up SQLite through its backup API, or stop the server before copying its database files. Copying a live `.db` alone can miss changes still in its WAL file.

The household model lets every signed-in member edit and delete shared rooms. Use it on a trusted local network. Public hosting needs stronger authentication, management roles, login rate limits, and HTTPS.

## Test

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
node --test tests/frontend.test.cjs
```

Tests use temporary databases and mocked Ollama responses. They never start the background simulation or require a GPU. Pytest only discovers `tests/`, so local audit scripts and output files are excluded. GitHub Actions runs the suite on Windows/Python 3.14 and Linux/Python 3.12.

`Update_Latest_Branch.bat` now updates the current branch using `git pull --ff-only`. It stops when tracked edits are present or histories diverge; it no longer switches to an arbitrary newest branch or forcibly resets local work.

See [the September audit](docs/audit-2026-09-12.md) for fixes, verification evidence, and recommendations.

## Third-party assets

Lucide 0.468.0 is vendored in `static/vendor/` with its license. System fonts and CSS backgrounds keep the interface independent of external asset CDNs.
