"""
transform.py - Phase 4: clean the raw legacy data according to the mapping in the README.

One clean_* function per table. Each takes the RAW table (all text) and returns:
    clean : DataFrame with the TARGET column names, ready to load (Phase 5)
    log   : DataFrame describing everything that did not load cleanly
            action = REJECT  row not loaded (reason code says why)
            action = MERGE   duplicate key merged into the first occurrence
            action = WARN    row loaded, but a value was blanked (e.g. unusable phone)

Rejects cascade down the chain, exactly like the foreign keys:
    meters are checked against the customers that LOADED,
    bills  are checked against the meters   that LOADED.

Run:  python transform.py     (cleans the CSVs, prints a summary, writes output/rejects.csv)
"""
import re
from decimal import Decimal
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data" / "legacy"
OUT_DIR = HERE / "output"

# ---------------------------------------------------------------------------
# Lookup tables. Keys are NORMALIZED (lower case, no dots, single spaces).
# ---------------------------------------------------------------------------
PROVINCE_MAP = {
    "pe": "PE", "pei": "PE", "prince edward island": "PE",
    "ns": "NS", "nova scotia": "NS",
    "nb": "NB", "new brunswick": "NB",
}
CUSTOMER_STATUS_MAP = {
    "a": "ACTIVE", "active": "ACTIVE",
    "i": "INACTIVE", "inactive": "INACTIVE",
    "c": "CLOSED", "closed": "CLOSED",
}
METER_STATUS_MAP = {
    "a": "ACTIVE", "active": "ACTIVE",
    "i": "INACTIVE", "inactive": "INACTIVE",
    "retired": "RETIRED",
}
METER_TYPE_MAP = {
    "water": "WATER", "wtr": "WATER",
    "elec": "ELECTRIC", "electric": "ELECTRIC",
    "gas": "GAS",
}

# The three legacy date formats, each with a regex so we never guess.
DATE_FORMATS = [
    ("%Y-%m-%d", r"\d{4}-\d{2}-\d{2}"),
    ("%m/%d/%Y", r"\d{2}/\d{2}/\d{4}"),        # month first, verified in profiling
    ("%d-%b-%Y", r"\d{2}-[A-Za-z]{3}-\d{4}"),
]

LOG_COLUMNS = ["table", "source_row", "source_key", "action", "reason", "detail"]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def normalize_key(s: pd.Series) -> pd.Series:
    """lower case, drop dots, collapse spaces: 'P.E.I.' -> 'pei', 'Nova  Scotia' -> 'nova scotia'."""
    return (s.str.strip().str.lower()
             .str.replace(".", "", regex=False)
             .str.replace(r"\s+", " ", regex=True))


def title_case(s: pd.Series) -> pd.Series:
    return s.str.strip().str.replace(r"\s+", " ", regex=True).str.title()


def parse_dates(s: pd.Series) -> pd.Series:
    """Try each legacy format only on values that LOOK like it. Anything else becomes NaT."""
    s = s.str.strip()
    result = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for fmt, pattern in DATE_FORMATS:
        matches = s.str.fullmatch(pattern)
        parsed = pd.to_datetime(s.where(matches), format=fmt, errors="coerce")
        result = result.combine_first(parsed)
    return result


def parse_decimal(s: pd.Series, places: int):
    """Text -> exact Decimal. Strips $ , and spaces.
    Returns (values, blank_mask, invalid_mask). Floats are never used, so no rounding drift."""
    t = s.str.replace(r"[$,\s]", "", regex=True)
    blank = t == ""
    valid = t.str.fullmatch(r"-?\d+(\.\d+)?")
    invalid = ~blank & ~valid
    quantum = Decimal(1).scaleb(-places)  # places=2 -> Decimal('0.01')
    values = pd.Series(
        [Decimal(x).quantize(quantum) if ok else None for x, ok in zip(t, valid)],
        index=s.index, dtype=object,
    )
    return values, blank, invalid


def clean_phone(s: pd.Series):
    """Digits only; drop a leading country code 1. Returns (phone_or_NaN, warn_mask)."""
    digits = s.str.replace(r"\D", "", regex=True)
    has_country_code = (digits.str.len() == 11) & digits.str.startswith("1")
    digits = digits.where(~has_country_code, digits.str[1:])
    ok = digits.str.len() == 10
    warn = ~ok & (s.str.strip() != "")          # something was there but unusable
    return digits.where(ok), warn


def to_nullable(s: pd.Series) -> pd.Series:
    """Real None (becomes SQL NULL) instead of NaN/NA."""
    return s.astype(object).where(s.notna(), None)


def dedupe(df: pd.DataFrame, key: str, table: str):
    """Keep the FIRST row for each key; log the rest as MERGE (survivorship rule: first occurrence)."""
    dup = df.duplicated(key, keep="first").to_numpy()
    first_row = (df.index.to_series() + 2).groupby(df[key]).transform("min").to_numpy()
    log = pd.DataFrame({
        "table": table,
        "source_row": (df.index[dup] + 2).to_numpy(),
        "source_key": df[key].to_numpy()[dup],
        "action": "MERGE",
        "reason": "MERGED_DUPLICATE",
        "detail": [f"kept source row {r}" for r in first_row[dup]],
    }, columns=LOG_COLUMNS)
    return df[~dup].copy(), log


class Flagger:
    """Collects the FIRST reject reason for each row (so a row is only counted once)."""

    def __init__(self, df: pd.DataFrame, table: str, key: str):
        self.df, self.table, self.key = df, table, key
        self.reason = pd.Series([None] * len(df), index=df.index, dtype=object)
        self.detail = pd.Series([None] * len(df), index=df.index, dtype=object)

    def flag(self, mask: pd.Series, code: str, raw: pd.Series):
        m = mask.fillna(False).astype(bool) & self.reason.isna()
        self.reason.loc[m] = code
        self.detail.loc[m] = raw.loc[m]

    @property
    def keep(self) -> pd.Series:
        return self.reason.isna()

    def log(self) -> pd.DataFrame:
        bad = self.reason.notna()
        return pd.DataFrame({
            "table": self.table,
            "source_row": (self.df.index[bad] + 2).to_numpy(),
            "source_key": self.df.loc[bad, self.key].to_numpy(),
            "action": "REJECT",
            "reason": self.reason[bad].to_numpy(),
            "detail": self.detail[bad].to_numpy(),
        }, columns=LOG_COLUMNS)


# ---------------------------------------------------------------------------
# One clean function per table
# ---------------------------------------------------------------------------
def clean_customers(raw: pd.DataFrame):
    df, merge_log = dedupe(raw, "CUST_ID", "customers")
    f = Flagger(df, "customers", "CUST_ID")

    acct = df["ACCT_NO"].str.strip()
    f.flag(acct == "", "MISSING_ACCT_NO", df["ACCT_NO"])
    f.flag(acct.duplicated(keep="first") & (acct != ""), "DUPLICATE_ACCT_NO", df["ACCT_NO"])  # schema says UNIQUE

    name, addr, city = title_case(df["FULL_NAME"]), df["SVC_ADDR"].str.strip(), title_case(df["CITY"])
    f.flag((name == "") | (addr == "") | (city == ""), "MISSING_REQUIRED", df["CUST_ID"])

    province = normalize_key(df["PROV"]).map(PROVINCE_MAP)
    f.flag(province.isna(), "UNKNOWN_PROVINCE", df["PROV"])

    postal = df["POSTAL"].str.replace(r"\s+", "", regex=True).str.upper()
    f.flag(~postal.str.fullmatch(r"[A-Z]\d[A-Z]\d[A-Z]\d"), "BAD_POSTAL", df["POSTAL"])

    status = normalize_key(df["STATUS"]).map(CUSTOMER_STATUS_MAP)
    f.flag(status.isna(), "UNKNOWN_STATUS", df["STATUS"])

    opened = parse_dates(df["OPEN_DT"])
    f.flag(opened.isna(), "BAD_DATE", df["OPEN_DT"])

    phone, phone_warn = clean_phone(df["PHONE"])
    email = df["EMAIL"].str.strip().str.lower()

    clean = pd.DataFrame({
        "cust_id": df["CUST_ID"].str.strip(),
        "acct_no": acct,
        "full_name": name,
        "phone": to_nullable(phone),
        "email": to_nullable(email.where(email != "")),
        "service_addr": addr,
        "city": city,
        "province": province,
        "postal_code": postal,
        "status": status,
        "open_date": opened.dt.date,
    })[f.keep].reset_index(drop=True)

    warn_rows = phone_warn & f.keep
    warn_log = pd.DataFrame({
        "table": "customers",
        "source_row": (df.index[warn_rows.to_numpy()] + 2).to_numpy(),
        "source_key": df.loc[warn_rows, "CUST_ID"].to_numpy(),
        "action": "WARN", "reason": "BAD_PHONE_BLANKED",
        "detail": df.loc[warn_rows, "PHONE"].to_numpy(),
    }, columns=LOG_COLUMNS)
    return clean, pd.concat([merge_log, f.log(), warn_log], ignore_index=True)


def clean_meters(raw: pd.DataFrame, loaded_customer_ids: set):
    df, merge_log = dedupe(raw, "METER_ID", "meters")
    f = Flagger(df, "meters", "METER_ID")

    cust_id = df["CUST_ID"].str.strip()
    f.flag(~cust_id.isin(loaded_customer_ids), "ORPHAN_CUSTOMER", df["CUST_ID"])

    mtype = normalize_key(df["METER_TYPE"]).map(METER_TYPE_MAP)
    f.flag(mtype.isna(), "UNKNOWN_METER_TYPE", df["METER_TYPE"])

    status = normalize_key(df["STATUS"]).map(METER_STATUS_MAP)
    f.flag(status.isna(), "UNKNOWN_STATUS", df["STATUS"])

    installed = parse_dates(df["INSTALL_DT"])
    f.flag(installed.isna(), "BAD_DATE", df["INSTALL_DT"])

    clean = pd.DataFrame({
        "meter_id": df["METER_ID"].str.strip(),
        "cust_id": cust_id,
        "meter_type": mtype,
        "install_date": installed.dt.date,
        "status": status,
    })[f.keep].reset_index(drop=True)
    return clean, pd.concat([merge_log, f.log()], ignore_index=True)


def clean_bills(raw: pd.DataFrame, loaded_meter_ids: set):
    df, merge_log = dedupe(raw, "BILL_ID", "bills")
    f = Flagger(df, "bills", "BILL_ID")

    meter_id = df["METER_ID"].str.strip()
    f.flag(~meter_id.isin(loaded_meter_ids), "ORPHAN_METER", df["METER_ID"])

    start = parse_dates(df["PERIOD_START"])
    end = parse_dates(df["PERIOD_END"])
    billed_on = parse_dates(df["BILL_DT"])
    f.flag(start.isna(), "BAD_DATE", df["PERIOD_START"])
    f.flag(end.isna(), "BAD_DATE", df["PERIOD_END"])
    f.flag(billed_on.isna(), "BAD_DATE", df["BILL_DT"])
    f.flag(end < start, "BAD_PERIOD", df["PERIOD_START"] + " > " + df["PERIOD_END"])

    usage, usage_blank, usage_bad = parse_decimal(df["USAGE"], places=1)
    f.flag(usage_blank | usage_bad, "BAD_USAGE", df["USAGE"])

    billed, billed_blank, billed_bad = parse_decimal(df["AMT_BILLED"], places=2)
    f.flag(billed_blank, "MISSING_AMOUNT", df["AMT_BILLED"])
    f.flag(billed_bad, "BAD_AMOUNT", df["AMT_BILLED"])

    paid, paid_blank, paid_bad = parse_decimal(df["AMT_PAID"], places=2)
    f.flag(paid_bad, "BAD_AMOUNT", df["AMT_PAID"])
    paid = paid.where(~paid_blank, Decimal("0.00"))      # blank = unpaid (documented assumption)

    clean = pd.DataFrame({
        "bill_id": df["BILL_ID"].str.strip(),
        "meter_id": meter_id,
        "period_start": start.dt.date,
        "period_end": end.dt.date,
        "bill_date": billed_on.dt.date,
        "usage_amount": usage,
        "amt_billed": billed,
        "amt_paid": paid,
    })[f.keep].reset_index(drop=True)
    return clean, pd.concat([merge_log, f.log()], ignore_index=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def load_raw() -> dict:
    raw = {}
    for name in ("customers", "meters", "bills"):
        path = DATA_DIR / f"{name}.csv"
        if not path.exists():
            raise SystemExit(f"Can't find {path} - run generate_legacy_data.py first.")
        # everything as text, blanks stay "" (no silent NaN conversion)
        raw[name] = pd.read_csv(path, dtype=str, keep_default_na=False)
    return raw


def transform(raw: dict):
    """customers first, then meters, then bills (parents before children)."""
    customers, log_c = clean_customers(raw["customers"])
    meters, log_m = clean_meters(raw["meters"], set(customers["cust_id"]))
    bills, log_b = clean_bills(raw["bills"], set(meters["meter_id"]))
    log = pd.concat([log_c, log_m, log_b], ignore_index=True)
    return {"customers": customers, "meters": meters, "bills": bills}, log


def print_summary(raw: dict, clean: dict, log: pd.DataFrame):
    print("=" * 66)
    print(f"{'table':<12}{'in file':>9}{'loaded':>9}{'merged':>9}{'rejected':>10}{'warnings':>10}")
    print("=" * 66)
    for t in ("customers", "meters", "bills"):
        sub = log[log["table"] == t]
        n = lambda a: int((sub["action"] == a).sum())
        print(f"{t:<12}{len(raw[t]):>9,}{len(clean[t]):>9,}{n('MERGE'):>9,}{n('REJECT'):>10,}{n('WARN'):>10,}")
        # every input row must be accounted for - nothing silently lost
        assert len(raw[t]) == len(clean[t]) + n("MERGE") + n("REJECT"), f"{t}: rows unaccounted for!"
    print("\nRows by reason:")
    for (t, action, reason), n in log.groupby(["table", "action", "reason"]).size().items():
        print(f"  {t:<10}{action:<8}{reason:<22}{n:>6,}")


def main():
    raw = load_raw()
    clean, log = transform(raw)
    print_summary(raw, clean, log)
    OUT_DIR.mkdir(exist_ok=True)
    log[log["action"] == "REJECT"].to_csv(OUT_DIR / "rejects.csv", index=False)
    log.to_csv(OUT_DIR / "transform_log.csv", index=False)
    print(f"\nWrote {OUT_DIR / 'rejects.csv'} and {OUT_DIR / 'transform_log.csv'}")
    print("\nSample of cleaned data:")
    for t, df in clean.items():
        print(f"\n[{t}]")
        print(df.head(3).to_string(index=False))


if __name__ == "__main__":
    main()
