# Trello weekly status extractor

Every Friday at 12:00 UTC a GitHub Actions workflow reads one Trello board per
student team and writes every card in the **Doing** and **Done** lists to a
shared Google Sheet. No always-on computer is needed.

The sheet gets these tabs:

| Tab | Content |
| --- | --- |
| `Tasks` | All tracked cards. Fully rewritten on every run. Bold frozen header row and a filter. |
| `Info` | Generation time (UTC) and how many cards each team has in Doing and Done. Failed boards are marked `ERROR`. |
| `2026-W42` etc. | Weekly snapshot named by ISO week, so history is kept. A rerun in the same week overwrites that week's tab. Can be switched off. |

Columns in `Tasks` and the snapshots:

| Column | Meaning |
| --- | --- |
| Team | Key from `BOARD_IDS` |
| Task | Card title |
| Status | `Doing` or `Done` |
| Members | Card members (full name, else username) |
| Started | First time the card entered a Doing list (created there or moved there) |
| Completed | Done cards only: latest time the card entered a Done list. If the board history has no such event, `dateLastActivity` is used |
| Due | Card due date |
| Last activity | Trello `dateLastActivity` |
| Card link | Short card URL |

Rows are sorted by team, then status (Doing before Done), then completion date.
All times are UTC.

## Files

- `trello_weekly.py` – the script
- `requirements.txt` – pinned dependencies (`requests`, `gspread`, `google-auth`)
- `../.github/workflows/trello-weekly.yml` – the scheduled workflow

## 1. Trello API key and token

1. Log in to Trello with an account that is a member of every team board (or
   make sure the boards are visible to that account).
2. Open <https://trello.com/power-ups/admin>, click **New**, fill in a name
   (e.g. `FYS330 weekly export`), pick your workspace and create it.
3. In the Power-Up, open **API key** and click **Generate a new API key**.
   Copy the **API key** (this is `TRELLO_KEY`). You do not need the secret.
4. Next to the key, click the **Token** link, or open this URL with your key
   filled in, and allow access:

   ```
   https://trello.com/1/authorize?expiration=never&scope=read&response_type=token&name=FYS330%20weekly%20export&key=YOUR_API_KEY
   ```

   Copy the token shown (this is `TRELLO_TOKEN`). Read scope is enough.

The token gives read access to everything the account can see. Revoke it at
<https://trello.com/my/account> (Applications) if it leaks.

## 2. Short board IDs

Open each board in the browser. The URL looks like

```
https://trello.com/b/AbCd1234/team-photon
```

The short ID is the part after `/b/`: `AbCd1234`.

`BOARD_IDS` is a JSON object mapping the team name shown in the sheet to the
short ID:

```json
{"Team 1": "AbCd1234", "Team 2": "EfGh5678", "Team 3": "IjKl9012"}
```

## 3. Google service account and sheet

1. Go to <https://console.cloud.google.com/>, create a project (a personal
   Gmail account works if your university Workspace blocks service account
   keys).
2. **APIs & Services → Library**: enable **Google Sheets API**.
3. **IAM & Admin → Service Accounts → Create service account**. Any name, no
   roles needed.
4. Open the service account, **Keys → Add key → Create new key → JSON**. A
   JSON file downloads. Its whole content is `GOOGLE_SERVICE_ACCOUNT_JSON`.
   Do not commit it; `.gitignore` excludes the usual file names.
5. Create the Google Sheet (or use an existing one). Click **Share** and add the
   service account's `client_email` (looks like
   `name@project.iam.gserviceaccount.com`) as **Editor**.
6. The sheet ID is in the sheet URL between `/d/` and `/edit`:

   ```
   https://docs.google.com/spreadsheets/d/THIS_IS_THE_SHEET_ID/edit
   ```

The script creates the `Tasks`, `Info` and snapshot tabs if they are missing.

## 4. Repository secrets

```
gh secret set TRELLO_KEY -R mikkojhuttunen/fys330-misc
gh secret set TRELLO_TOKEN -R mikkojhuttunen/fys330-misc
gh secret set BOARD_IDS -R mikkojhuttunen/fys330-misc --body '{"Team 1": "AbCd1234", "Team 2": "EfGh5678"}'
gh secret set GOOGLE_SERVICE_ACCOUNT_JSON -R mikkojhuttunen/fys330-misc < path/to/service-account.json
gh secret set SHEET_ID -R mikkojhuttunen/fys330-misc
```

`gh secret set NAME` without `--body` prompts for the value, so it does not end
up in your shell history.

### Optional settings (repository variables)

These are not secret, so they are repository variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DOING_LISTS` | `doing` | Comma-separated list names counted as Doing (case-insensitive) |
| `DONE_LISTS` | `done` | Comma-separated list names counted as Done (case-insensitive) |
| `WEEKLY_SNAPSHOT` | `true` | `false` disables the weekly snapshot tab |

```
gh variable set DOING_LISTS -R mikkojhuttunen/fys330-misc --body "doing,in progress"
gh variable set DONE_LISTS -R mikkojhuttunen/fys330-misc --body "done,valmis"
gh variable set WEEKLY_SNAPSHOT -R mikkojhuttunen/fys330-misc --body "false"
```

## 5. Running

Manual run and following it live:

```
gh workflow run trello-weekly.yml -R mikkojhuttunen/fys330-misc
gh run watch -R mikkojhuttunen/fys330-misc
```

Or in the browser: **Actions → Trello weekly export → Run workflow**.

Notes:

- Scheduled and manual runs use the workflow file on the default branch, so it
  must be merged there first.
- GitHub may start scheduled runs some minutes late.
- In a public repository GitHub disables scheduled workflows after 60 days
  without repository activity. Re-enable it in the Actions tab if that happens.

### Failures

If a board fails (wrong ID, no access, 401/404), the log shows the team name
and the HTTP status, the other boards are still exported, the `Info` tab marks
the team as `ERROR`, and the run ends with a non-zero exit code so the workflow
shows red. That team's rows are missing from `Tasks` for that run. If every
board fails, the sheet is left untouched.

### Privacy

The repository is public, so workflow logs are public. The script logs only team
names, card counts and error codes, never card content or secrets, and the
workflow uploads no artifacts. `--dry-run` refuses to print card data when it
detects it is running inside GitHub Actions.

## 6. Local dry run

Prints the rows to the terminal instead of writing to the sheet. Google
variables are not needed.

```
cd trello-weekly
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export TRELLO_KEY=your_key
export TRELLO_TOKEN=your_token
export BOARD_IDS='{"Team 1": "AbCd1234"}'
python trello_weekly.py --dry-run
```

Exit codes: `0` all good, `1` at least one board or the sheet write failed, `2`
configuration error.

## 7. Adding a new team

1. Make sure the Trello account behind the token can see the new board.
2. Update the secret with the full mapping, including the new team:

   ```
   gh secret set BOARD_IDS -R mikkojhuttunen/fys330-misc --body '{"Team 1": "AbCd1234", "Team 2": "EfGh5678", "Team 7": "MnOp3456"}'
   ```

3. Trigger a manual run to check it, see section 5.

Secrets cannot be read back, so keep a copy of the current `BOARD_IDS` value
somewhere private (board IDs are not very sensitive, but keep them out of the
public repo).
