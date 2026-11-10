"""
etl.py - Phases 5 + 6: run the whole pipeline end to end.

    EXTRACT    read the raw legacy CSVs as text
    TRANSFORM  clean them (transform.py), logging every reject / merge / warning
    LOAD       rebuild the MySQL tables from schema.sql, then load customers -> meters -> bills
               in ONE transaction (all three tables load, or none do)
    RECONCILE  prove nothing was lost: row counts, money totals, foreign keys

Re-runnable: every run drops and recreates the three tables first (a full refresh),
so running it twice gives the same result and never duplicates rows.

Connection settings come from environment variables or a local .env file (never commit it):
    MYSQL_PASSWORD (required)   MYSQL_USER (default etl_user)   MYSQL_HOST (default 127.0.0.1)
    MYSQL_PORT (default 3306)   MYSQL_DB (default utility_billing)

Run:  python etl.py
Out:  console + output/run_summary.txt, output/rejects.csv, output/transform_log.csv
Exit code is 0 if every reconciliation check passes, 1 otherwise.
"""
import contextlib
import io
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

import transform as T

HERE = Path(__file__).resolve().parent
SCHEMA_FILE = HERE / "schema.sql"
OUT_DIR = HERE / "output"
ANSWER_KEY = T.DATA_DIR / "_answer_key.json"
TABLE_ORDER = ["customers", "meters", "bills"]      # parents before children


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def load_dotenv(path: Path = HERE / ".env"):
    """Tiny .env reader so we don't need another dependency. Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_engine():
    load_dotenv()
    password = os.environ.get("MYSQL_PASSWORD")
    if not password:
        sys.exit("MYSQL_PASSWORD is not set.\n"
                 "Create a file named .env in the project folder containing the line:\n"
                 "    MYSQL_PASSWORD=your-etl_user-password\n"
                 "(.env is in .gitignore, so it will not be pushed to GitHub.)")
    url = URL.create(                       # URL.create handles special characters in passwords
        "mysql+pymysql",
        username=os.environ.get("MYSQL_USER", "etl_user"),
        password=password,
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        database=os.environ.get("MYSQL_DB", "utility_billing"),
    )
    return create_engine(url)


def check_connection(engine):
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception as exc:                # show a friendly hint, then the real error
        msg = str(exc)
        hint = "Check the MySQL connection settings."
        if "Access denied" in msg:
            hint = "Wrong user or password. Check MYSQL_PASSWORD in .env matches the ALTER USER you ran."
        elif "Can't connect" in msg or "2003" in msg:
            hint = "MySQL isn't reachable. Is the MySQL service running (Workbench's Server Status shows it)?"
        elif "Unknown database" in msg:
            hint = "The utility_billing database doesn't exist. Run setup_mysql.sql in Workbench first."
        sys.exit(f"Could not connect to MySQL.\n  Hint: {hint}\n  Details: {msg.splitlines()[0]}")


# ---------------------------------------------------------------------------
# Schema + load
# ---------------------------------------------------------------------------
def split_sql(sql: str):
    """Drop -- comments, split on ';'."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [s.strip() for s in no_comments.split(";") if s.strip()]


def create_schema(engine):
    statements = split_sql(SCHEMA_FILE.read_text(encoding="utf-8"))
    with engine.begin() as conn:
        for stmt in statements:
            if stmt.split()[0].upper() in ("USE", "SHOW"):   # connection already targets the database
                continue
            conn.exec_driver_sql(stmt)


def load_tables(engine, clean: dict):
    """One transaction: if any table fails, nothing is left half-loaded."""
    with engine.begin() as conn:
        for table in TABLE_ORDER:
            clean[table].to_sql(table, conn, if_exists="append", index=False,
                                chunksize=1000, method="multi")


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def money(x) -> Decimal:
    return Decimal(str(x if x is not None else 0)).quantize(Decimal("0.01"))


def sum_amounts(series: pd.Series) -> Decimal:
    values, _blank, _bad = T.parse_decimal(series, places=2)
    return sum(values.dropna(), Decimal("0.00"))        # blanks count as 0.00


def fmt(v) -> str:
    return f"{v:,.2f}" if isinstance(v, Decimal) else f"{int(v):,}"


def reconcile(engine, raw: dict, clean: dict, log: pd.DataFrame):
    checks = []

    def check(name, expected, actual):
        checks.append((name, expected, actual, expected == actual))

    with engine.connect() as conn:
        scalar = lambda sql: conn.exec_driver_sql(sql).scalar()
        db_count = {t: int(scalar(f"SELECT COUNT(*) FROM {t}")) for t in TABLE_ORDER}
        db_billed = money(scalar("SELECT SUM(amt_billed) FROM bills"))
        db_paid = money(scalar("SELECT SUM(amt_paid) FROM bills"))
        orphan_meters = int(scalar("SELECT COUNT(*) FROM meters m LEFT JOIN customers c "
                                   "ON c.cust_id = m.cust_id WHERE c.cust_id IS NULL"))
        orphan_bills = int(scalar("SELECT COUNT(*) FROM bills b LEFT JOIN meters m "
                                  "ON m.meter_id = b.meter_id WHERE m.meter_id IS NULL"))

    # 1. Row accounting: every row in the file is loaded, merged or rejected
    for t in TABLE_ORDER:
        sub = log[log["table"] == t]
        merged = int((sub["action"] == "MERGE").sum())
        rejected = int((sub["action"] == "REJECT").sum())
        check(f"{t}: rows in file = loaded + merged + rejected", len(raw[t]), db_count[t] + merged + rejected)
        check(f"{t}: rows in MySQL = rows the transform produced", len(clean[t]), db_count[t])

    # 2. Money accounting: what the file billed = what we loaded + what we set aside
    bills_raw = raw["bills"]
    dropped = log[(log["table"] == "bills") & (log["action"].isin(["REJECT", "MERGE"]))]
    dropped_rows = bills_raw.loc[(dropped["source_row"] - 2).to_numpy()]
    check("bills: total billed in file = loaded + rejected", sum_amounts(bills_raw["AMT_BILLED"]),
          db_billed + sum_amounts(dropped_rows["AMT_BILLED"]))
    check("bills: total paid in file   = loaded + rejected", sum_amounts(bills_raw["AMT_PAID"]),
          db_paid + sum_amounts(dropped_rows["AMT_PAID"]))

    # 3. Relationship integrity inside MySQL
    check("meters pointing at a missing customer", 0, orphan_meters)
    check("bills pointing at a missing meter", 0, orphan_bills)

    # 4. Synthetic-data bonus: compare with the answer key written by the generator
    if ANSWER_KEY.exists():
        key = json.loads(ANSWER_KEY.read_text())
        for t in TABLE_ORDER:
            check(f"{t}: loaded = answer key (synthetic data only)", key[t]["expected_loaded"], db_count[t])
    return checks


def print_checks(checks, out):
    out("=" * 78)
    out("RECONCILIATION")
    out("=" * 78)
    for name, expected, actual, ok in checks:
        out(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok or isinstance(expected, Decimal):
            out(f"        expected {fmt(expected)}   actual {fmt(actual)}")
    passed = sum(1 for c in checks if c[3])
    out(f"\n{passed} of {len(checks)} checks passed.")
    return passed == len(checks)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(engine=None) -> bool:
    lines = []

    def out(text=""):
        print(text)
        lines.append(text)

    timings = {}

    def timed(label, fn, *args):
        start = time.perf_counter()
        result = fn(*args)
        timings[label] = time.perf_counter() - start
        return result

    engine = engine or get_engine()
    check_connection(engine)

    raw = timed("extract", T.load_raw)
    clean, log = timed("transform", T.transform, raw)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        T.print_summary(raw, clean, log)
    out(buffer.getvalue().rstrip())
    out()

    timed("create schema", create_schema, engine)
    timed("load", load_tables, engine, clean)
    out("Loaded: " + ", ".join(f"{len(clean[t]):,} {t}" for t in TABLE_ORDER))
    out()

    checks = timed("reconcile", reconcile, engine, raw, clean, log)
    ok = print_checks(checks, out)

    out("\nStage timings (seconds): " + ", ".join(f"{k} {v:.2f}" for k, v in timings.items()))
    OUT_DIR.mkdir(exist_ok=True)
    log[log["action"] == "REJECT"].to_csv(OUT_DIR / "rejects.csv", index=False)
    log.to_csv(OUT_DIR / "transform_log.csv", index=False)
    (OUT_DIR / "run_summary.txt").write_text("\n".join(lines), encoding="utf-8")
    out(f"Wrote run_summary.txt, rejects.csv and transform_log.csv to {OUT_DIR}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
