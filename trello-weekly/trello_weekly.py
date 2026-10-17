#!/usr/bin/env python3
"""Weekly Trello status extractor.

Reads one Trello board per student team and writes every card that sits in a
"Doing" or "Done" list into a shared Google Sheet, with start and completion
dates derived from the board's action history.

All configuration comes from environment variables (see README.md).
Secrets are never printed. Card content is only printed in --dry-run mode,
and never when running inside GitHub Actions.
"""

import argparse
import datetime as dt
import json
import os
import sys

import requests

TRELLO_API = "https://api.trello.com/1"
ACTIONS_PAGE_LIMIT = 1000
ACTIONS_MAX_PAGES = 10
HTTP_TIMEOUT = 30

HEADER = [
    "Team",
    "Task",
    "Status",
    "Members",
    "Started",
    "Completed",
    "Due",
    "Last activity",
    "Card link",
]
DATE_COLUMNS = [HEADER.index(c) for c in ("Started", "Completed", "Due", "Last activity")]
TEXT_COLUMNS = [HEADER.index(c) for c in ("Team", "Task", "Members")]

TASKS_TAB = "Tasks"
INFO_TAB = "Info"


class ConfigError(Exception):
    pass


class BoardError(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def parse_bool(value, default):
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def parse_list_names(value, default):
    if value is None or value.strip() == "":
        return {default}
    names = {n.strip().lower() for n in value.split(",") if n.strip()}
    return names or {default}


def load_config(dry_run):
    def require(name):
        value = os.environ.get(name, "").strip()
        if not value:
            raise ConfigError(f"environment variable {name} is not set")
        return value

    cfg = {
        "trello_key": require("TRELLO_KEY"),
        "trello_token": require("TRELLO_TOKEN"),
        "doing_lists": parse_list_names(os.environ.get("DOING_LISTS"), "doing"),
        "done_lists": parse_list_names(os.environ.get("DONE_LISTS"), "done"),
        "weekly_snapshot": parse_bool(os.environ.get("WEEKLY_SNAPSHOT"), True),
    }

    try:
        boards = json.loads(require("BOARD_IDS"))
    except json.JSONDecodeError:
        raise ConfigError('BOARD_IDS is not valid JSON, expected e.g. {"Team A": "abc123XY"}') from None
    if not isinstance(boards, dict) or not boards:
        raise ConfigError("BOARD_IDS must be a non-empty JSON object mapping team name to board ID")
    for team, board_id in boards.items():
        if not isinstance(board_id, str) or not board_id.strip():
            raise ConfigError(f"BOARD_IDS: board ID for team {team!r} must be a non-empty string")
    cfg["boards"] = {str(t): b.strip() for t, b in boards.items()}

    if cfg["doing_lists"] & cfg["done_lists"]:
        raise ConfigError("DOING_LISTS and DONE_LISTS must not share list names")

    if not dry_run:
        try:
            cfg["service_account"] = json.loads(require("GOOGLE_SERVICE_ACCOUNT_JSON"))
        except json.JSONDecodeError:
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from None
        cfg["sheet_id"] = require("SHEET_ID")
    return cfg


class Trello:
    def __init__(self, key, token):
        self.session = requests.Session()
        # Credentials go in a header, not the URL, so they never show up in
        # exception messages or tracebacks.
        self.session.headers["Authorization"] = (
            f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"'
        )

    def get(self, path, **params):
        try:
            resp = self.session.get(f"{TRELLO_API}{path}", params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise BoardError(f"network error ({type(exc).__name__})") from None
        if resp.status_code != 200:
            hint = {
                400: "bad request, check the board ID",
                401: "unauthorized, check TRELLO_KEY/TRELLO_TOKEN and board access",
                404: "not found, check the board ID",
                429: "rate limited",
            }.get(resp.status_code, resp.reason or "error")
            raise BoardError(f"HTTP {resp.status_code} ({hint})")
        try:
            return resp.json()
        except ValueError:
            raise BoardError("invalid JSON from Trello") from None

    def board(self, board_id):
        base = f"/boards/{board_id}"
        lists = self.get(f"{base}/lists", filter="open", fields="name")
        members = self.get(f"{base}/members", fields="fullName,username")
        cards = self.get(
            f"{base}/cards",
            filter="open",
            fields="name,idList,idMembers,due,dateLastActivity,shortUrl",
        )
        actions = []
        before = None
        for _ in range(ACTIONS_MAX_PAGES):
            params = {"filter": "createCard,updateCard:idList", "limit": ACTIONS_PAGE_LIMIT}
            if before:
                params["before"] = before
            page = self.get(f"{base}/actions", **params)
            actions.extend(page)
            if len(page) < ACTIONS_PAGE_LIMIT:
                break
            before = page[-1]["id"]
        return lists, members, cards, actions


def parse_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_ts(value):
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def classify(name, cfg):
    key = (name or "").strip().lower()
    if key in cfg["doing_lists"]:
        return "Doing"
    if key in cfg["done_lists"]:
        return "Done"
    return None


def list_entries(actions, list_status, cfg):
    """Return {card_id: [(timestamp, status), ...]} for every time a card
    entered a tracked list, via creation or a move."""
    entries = {}
    for action in actions:
        data = action.get("data") or {}
        card = data.get("card") or {}
        if action.get("type") == "createCard":
            target = data.get("list") or {}
        elif action.get("type") == "updateCard" and "listAfter" in data:
            target = data.get("listAfter") or {}
        else:
            continue
        status = list_status.get(target.get("id")) or classify(target.get("name"), cfg)
        when = parse_ts(action.get("date"))
        if status and when and card.get("id"):
            entries.setdefault(card["id"], []).append((when, status))
    return entries


def build_rows(team, lists, members, cards, actions, cfg):
    list_status = {}
    for lst in lists:
        status = classify(lst.get("name"), cfg)
        if status:
            list_status[lst["id"]] = status
    member_names = {m["id"]: m.get("fullName") or m.get("username") or "" for m in members}
    entries = list_entries(actions, list_status, cfg)

    rows = []
    for card in cards:
        status = list_status.get(card.get("idList"))
        if not status:
            continue
        card_entries = entries.get(card["id"], [])
        doing_times = [t for t, s in card_entries if s == "Doing"]
        done_times = [t for t, s in card_entries if s == "Done"]
        last_activity = parse_ts(card.get("dateLastActivity"))

        started = min(doing_times) if doing_times else None
        completed = None
        if status == "Done":
            completed = max(done_times) if done_times else last_activity

        names = sorted(member_names.get(mid, "(former member)") for mid in card.get("idMembers", []))
        rows.append(
            {
                "Team": team,
                "Task": card.get("name", ""),
                "Status": status,
                "Members": ", ".join(names),
                "Started": started,
                "Completed": completed,
                "Due": parse_ts(card.get("due")),
                "Last activity": last_activity,
                "Card link": card.get("shortUrl", ""),
            }
        )
    return rows


def sort_rows(rows):
    status_order = {"Doing": 0, "Done": 1}
    far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    rows.sort(
        key=lambda r: (
            r["Team"].lower(),
            status_order.get(r["Status"], 9),
            r["Completed"] or far_future,
            r["Task"].lower(),
        )
    )
    return rows


def safe_text(value):
    # The sheet is written with USER_ENTERED so dates become real dates;
    # prefix text that Sheets would otherwise evaluate as a formula.
    if value and value[0] in "=+-@":
        return "'" + value
    return value


def to_values(rows):
    values = [HEADER]
    for r in rows:
        line = []
        for i, col in enumerate(HEADER):
            v = r[col]
            if i in DATE_COLUMNS:
                v = fmt_ts(v)
            elif i in TEXT_COLUMNS:
                v = safe_text(v)
            line.append(v)
        values.append(line)
    return values


def info_values(generated, results):
    values = [
        ["Generated (UTC)", generated.strftime("%Y-%m-%d %H:%M:%S")],
        [],
        ["Team", "Doing", "Done", "Status"],
    ]
    for team, res in results.items():
        if res["error"]:
            values.append([safe_text(team), "", "", f"ERROR: {res['error']}"])
        else:
            doing = sum(1 for r in res["rows"] if r["Status"] == "Doing")
            done = sum(1 for r in res["rows"] if r["Status"] == "Done")
            values.append([safe_text(team), doing, done, "OK"])
    return values


def week_tab_name(when):
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def get_or_add_worksheet(spreadsheet, title, rows, cols):
    import gspread

    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def write_table(spreadsheet, title, values, date_columns=(), filter_and_freeze=True):
    nrows = max(len(values), 2)
    ncols = max(len(r) for r in values)
    ws = get_or_add_worksheet(spreadsheet, title, nrows, ncols)
    sid = ws.id

    requests_ = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sid,
                    "gridProperties": {
                        "rowCount": nrows,
                        "columnCount": ncols,
                        "frozenRowCount": 1 if filter_and_freeze else 0,
                    },
                },
                "fields": "gridProperties(rowCount,columnCount,frozenRowCount)",
            }
        },
        {"updateCells": {"range": {"sheetId": sid}, "fields": "userEnteredValue,userEnteredFormat"}},
    ]
    if filter_and_freeze:
        requests_.append(
            {
                "repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            }
        )
        requests_.append(
            {
                "setBasicFilter": {
                    "filter": {
                        "range": {
                            "sheetId": sid,
                            "startRowIndex": 0,
                            "endRowIndex": len(values),
                            "startColumnIndex": 0,
                            "endColumnIndex": ncols,
                        }
                    }
                }
            }
        )
    for col in date_columns:
        requests_.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 1,
                        "startColumnIndex": col,
                        "endColumnIndex": col + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm"}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        )

    spreadsheet.batch_update({"requests": requests_})
    ws.update(values=values, range_name="A1", value_input_option="USER_ENTERED")
    return ws


def write_sheet(cfg, generated, rows, results):
    import gspread

    client = gspread.service_account_from_dict(cfg["service_account"])
    spreadsheet = client.open_by_key(cfg["sheet_id"])

    task_values = to_values(rows)
    write_table(spreadsheet, TASKS_TAB, task_values, DATE_COLUMNS)
    log(f"Wrote {len(rows)} rows to tab {TASKS_TAB!r}")

    write_table(spreadsheet, INFO_TAB, info_values(generated, results), filter_and_freeze=False)
    log(f"Wrote tab {INFO_TAB!r}")

    if cfg["weekly_snapshot"]:
        tab = week_tab_name(generated)
        write_table(spreadsheet, tab, task_values, DATE_COLUMNS)
        log(f"Wrote snapshot tab {tab!r}")


def print_rows(rows, results, generated):
    values = to_values(rows)
    widths = [min(max(len(str(r[i])) for r in values), 40) for i in range(len(HEADER))]
    for line in values:
        print("  ".join(str(v)[:40].ljust(w) for v, w in zip(line, widths)).rstrip())
    print()
    for line in info_values(generated, results):
        print("  ".join(str(v) for v in line))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export Trello Doing/Done cards to Google Sheets.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print rows to the terminal instead of writing to the sheet",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.dry_run)
    except ConfigError as exc:
        log(f"Configuration error: {exc}")
        return 2

    generated = dt.datetime.now(dt.timezone.utc)
    trello = Trello(cfg["trello_key"], cfg["trello_token"])
    results = {}
    all_rows = []
    for team, board_id in cfg["boards"].items():
        try:
            lists, members, cards, actions = trello.board(board_id)
            rows = build_rows(team, lists, members, cards, actions, cfg)
        except BoardError as exc:
            log(f"[{team}] FAILED: {exc}")
            results[team] = {"rows": [], "error": str(exc)}
            continue
        except (KeyError, TypeError, AttributeError) as exc:
            log(f"[{team}] FAILED: unexpected data from Trello ({type(exc).__name__})")
            results[team] = {"rows": [], "error": "unexpected data from Trello"}
            continue
        doing = sum(1 for r in rows if r["Status"] == "Doing")
        log(f"[{team}] OK: {doing} doing, {len(rows) - doing} done")
        results[team] = {"rows": rows, "error": None}
        all_rows.extend(rows)

    sort_rows(all_rows)
    failed = [t for t, r in results.items() if r["error"]]

    if args.dry_run:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            log("Dry run inside GitHub Actions: not printing card data to public logs")
        else:
            print_rows(all_rows, results, generated)
    elif len(failed) == len(results):
        log("All boards failed, leaving the sheet untouched")
    else:
        import gspread

        try:
            write_sheet(cfg, generated, all_rows, results)
        except gspread.SpreadsheetNotFound:
            log("Writing to Google Sheets failed: sheet not found, check SHEET_ID and "
                "that the sheet is shared with the service account email as Editor")
            return 1
        except gspread.exceptions.APIError as exc:
            log(f"Writing to Google Sheets failed: HTTP {exc.code} {exc.error.get('status', '')}")
            return 1
        except Exception as exc:
            log(f"Writing to Google Sheets failed: {type(exc).__name__}")
            return 1

    if failed:
        log(f"{len(failed)} board(s) failed: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
