"""
profile.py - Phase 2: profile the legacy CSVs BEFORE writing any cleaning code.

For each table it reports:
  * row count and exact-duplicate rows
  * per column: blank count, distinct count, most common values
  * "shape" analysis for messy columns (dates, phones, postal codes, amounts):
        letters -> A/a, digits -> 9, so "(902) 555-0123" becomes "(999) 999-9999".
        Counting shapes shows every format hiding in a column.
  * key checks: duplicate primary keys
  * foreign-key checks: orphan meters (customer missing), orphan bills (meter missing)

Run:  python profile.py
Out:  console + output/profile_report.txt

Your job: read the report, then write 5 findings in your own words (see README).
"""
import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data" / "legacy"
OUT_DIR = HERE / "output"

TABLES = {
    "customers": {"pk": "CUST_ID", "shape_cols": ["ACCT_NO", "PHONE", "POSTAL", "OPEN_DT"],
                  "category_cols": ["PROV", "STATUS", "CITY"]},
    "meters":    {"pk": "METER_ID", "shape_cols": ["INSTALL_DT"],
                  "category_cols": ["METER_TYPE", "STATUS"]},
    "bills":     {"pk": "BILL_ID", "shape_cols": ["PERIOD_START", "PERIOD_END", "BILL_DT", "AMT_BILLED", "AMT_PAID"],
                  "category_cols": []},
}


class Report:
    """Print to the console and keep a copy for the report file."""
    def __init__(self):
        self.lines = []

    def add(self, text=""):
        print(text)
        self.lines.append(text)

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines), encoding="utf-8")


def shape(value: str) -> str:
    """Turn a value into its format pattern: 'ab-12' -> 'aa-99'."""
    s = re.sub(r"[A-Z]", "A", value)
    s = re.sub(r"[a-z]", "a", s)
    return re.sub(r"[0-9]", "9", s)


def load(name: str) -> pd.DataFrame:
    path = DATA_DIR / f"{name}.csv"
    if not path.exists():
        sys.exit(f"Can't find {path}\nRun generate_legacy_data.py first, from this project folder "
                 f"(in PyCharm: Run > Edit Configurations > Working directory = project folder).")
    # dtype=str + keep_default_na=False: read EVERYTHING as raw text so pandas can't hide problems
    # (e.g. turning blank cells into NaN, or "007" into 7).
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def profile_table(name: str, df: pd.DataFrame, cfg: dict, rpt: Report):
    rpt.add("=" * 70)
    rpt.add(f"TABLE: {name}   ({len(df):,} rows, {len(df.columns)} columns)")
    rpt.add("=" * 70)

    rpt.add(f"Exact duplicate rows: {df.duplicated().sum():,}")
    pk = cfg["pk"]
    rpt.add(f"Rows sharing a {pk} with another row: {df.duplicated(pk, keep=False).sum():,} "
            f"(extra copies: {df.duplicated(pk).sum():,})")

    rpt.add("\n-- Blank / distinct counts per column --")
    rpt.add(f"{'column':<14}{'blank':>8}{'distinct':>10}")
    for col in df.columns:
        blank = (df[col].str.strip() == "").sum()
        rpt.add(f"{col:<14}{blank:>8,}{df[col].nunique():>10,}")

    for col in cfg["category_cols"]:
        rpt.add(f"\n-- Values in {col} --")
        for val, n in df[col].value_counts().items():
            rpt.add(f"  {val!r:<26}{n:>7,}")

    for col in cfg["shape_cols"]:
        rpt.add(f"\n-- Formats (shapes) in {col} --")
        shapes = df[col].map(shape).value_counts()
        for shp, n in shapes.head(12).items():
            example = df.loc[df[col].map(shape) == shp, col].iloc[0]
            rpt.add(f"  {shp!r:<28}{n:>7,}   e.g. {example!r}")
        if len(shapes) > 12:
            rpt.add(f"  ... plus {len(shapes) - 12} more shapes")
    rpt.add()


def check_foreign_keys(customers, meters, bills, rpt: Report):
    rpt.add("=" * 70)
    rpt.add("RELATIONSHIP CHECKS")
    rpt.add("=" * 70)
    cust_ids = set(customers["CUST_ID"])
    meter_ids = set(meters["METER_ID"])

    orphan_meters = ~meters["CUST_ID"].isin(cust_ids)
    rpt.add(f"Meters whose CUST_ID is not in customers: {orphan_meters.sum():,}")
    if orphan_meters.any():
        rpt.add(f"   examples: {meters.loc[orphan_meters, 'CUST_ID'].head(3).tolist()}")

    orphan_bills = ~bills["METER_ID"].isin(meter_ids)
    rpt.add(f"Bills whose METER_ID is not in meters:     {orphan_bills.sum():,}")
    if orphan_bills.any():
        rpt.add(f"   examples: {bills.loc[orphan_bills, 'METER_ID'].head(3).tolist()}")

    no_meter = ~customers["CUST_ID"].isin(set(meters["CUST_ID"]))
    rpt.add(f"Customers with no meters (not necessarily a problem): {no_meter.sum():,}")
    rpt.add()


DATE_COLS = {"customers": ["OPEN_DT"], "meters": ["INSTALL_DT"], "bills": ["PERIOD_START", "PERIOD_END", "BILL_DT"]}


def check_slash_dates(data: dict, rpt: Report):
    """Slash dates (99/99/9999) are ambiguous: is it MM/DD or DD/MM?
    Look at the two leading numbers. A value above 12 can only be a DAY, so it reveals the order."""
    rpt.add("=" * 70)
    rpt.add("AMBIGUOUS DATE CHECK (values shaped 99/99/9999)")
    rpt.add("=" * 70)
    first_gt12 = second_gt12 = total = 0
    for table, cols in DATE_COLS.items():
        for col in cols:
            s = data[table][col]
            s = s[s.str.fullmatch(r"\d{2}/\d{2}/\d{4}")]
            a = s.str[0:2].astype(int)
            b = s.str[3:5].astype(int)
            total += len(s)
            first_gt12 += (a > 12).sum()
            second_gt12 += (b > 12).sum()
    rpt.add(f"Slash-format dates checked: {total:,}")
    rpt.add(f"  first number > 12 (must be a day, so DD/MM): {first_gt12:,}")
    rpt.add(f"  second number > 12 (must be a day, so MM/DD): {second_gt12:,}")
    rpt.add("  If only one of the two is non-zero, that is the order. Confirm with the source system owner.")
    rpt.add()


def main():
    rpt = Report()
    data = {name: load(name) for name in TABLES}
    for name, cfg in TABLES.items():
        profile_table(name, data[name], cfg, rpt)
    check_foreign_keys(data["customers"], data["meters"], data["bills"], rpt)
    check_slash_dates(data, rpt)
    rpt.save(OUT_DIR / "profile_report.txt")
    print(f"Report saved to {OUT_DIR / 'profile_report.txt'}")


if __name__ == "__main__":
    main()
