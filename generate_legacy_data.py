"""
generate_legacy_data.py

Generates a fake "legacy utility billing system" export: three CSV files with
realistic data-quality problems, plus an answer key listing exactly how many
defects were planted. The ETL pipeline (etl.py) should catch all of them, and
the reconciliation step compares its numbers against the answer key.

Run:  python generate_legacy_data.py
Out:  data/legacy/customers.csv, meters.csv, bills.csv
      data/legacy/_answer_key.json   (planted defect counts - don't peek while building!)

All data is synthetic. Seeded, so every run produces identical files.
"""
import json
import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from faker import Faker

SEED = 42
N_CUSTOMERS = 2000
N_DUP_CUSTOMERS = 120        # duplicate customer rows (same CUST_ID / ACCT_NO)
N_BLANK_ACCT = 40            # customers with no account number
N_METERS = 2500
N_ORPHAN_METERS = 60         # meters pointing at a customer that doesn't exist
N_BILLS = 10000
N_ORPHAN_BILLS = 150         # bills pointing at a meter that doesn't exist
N_NULL_AMOUNT_BILLS = 30     # bills with no billed amount

OUT_DIR = Path("data/legacy")

random.seed(SEED)
Faker.seed(SEED)
fake = Faker("en_CA")

# city -> canonical province code
CITIES = {
    "Charlottetown": "PE", "Summerside": "PE", "Stratford": "PE", "Cornwall": "PE",
    "Halifax": "NS", "Dartmouth": "NS", "Sydney": "NS",
    "Moncton": "NB", "Fredericton": "NB", "Saint John": "NB",
}
AREA_CODES = {"PE": "902", "NS": "902", "NB": "506"}
PROV_VARIANTS = {
    "PE": ["PE", "PE", "PE", "P.E.I.", "PEI", "Prince Edward Island", "pe"],
    "NS": ["NS", "NS", "NS", "N.S.", "Nova Scotia", "ns"],
    "NB": ["NB", "NB", "NB", "N.B.", "New Brunswick", "nb"],
}
STATUS_VARIANTS = ["A", "A", "ACTIVE", "Active", "I", "INACTIVE", "C", "CLOSED"]
METER_TYPE_VARIANTS = {
    "WATER": ["WATER", "WATER", "WTR", "water"],
    "ELECTRIC": ["ELECTRIC", "ELEC", "electric", "ELEC"],
    "GAS": ["GAS", "GAS", "gas"],
}
RATES = {"WATER": (0.0042, 18.0), "ELECTRIC": (0.1350, 22.0), "GAS": (0.5200, 15.0)}  # per unit, fixed fee


# ---------- helpers that deliberately create messy formats ----------
def messy_date(d: date) -> str:
    """Three legacy date formats. m/d/Y is always month-first (never d/m/Y)."""
    fmt = random.choice(["%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y"])
    return d.strftime(fmt)


def messy_phone(prov: str) -> str:
    area = AREA_CODES[prov]
    num = f"{random.randint(200, 999)}{random.randint(0, 9999):04d}"
    d = area + num
    style = random.choice(["paren", "dash", "plain", "plus1", "dot"])
    return {
        "paren": f"({d[:3]}) {d[3:6]}-{d[6:]}",
        "dash": f"{d[:3]}-{d[3:6]}-{d[6:]}",
        "plain": d,
        "plus1": f"+1 {d[:3]} {d[3:6]} {d[6:]}",
        "dot": f"{d[:3]}.{d[3:6]}.{d[6:]}",
    }[style]


def messy_postal() -> str:
    letters, digits = "ABCEGHJKLMNPRSTVXY", "0123456789"
    p = (random.choice(letters) + random.choice(digits) + random.choice(letters)
         + random.choice(digits) + random.choice(letters) + random.choice(digits))
    return random.choice([f"{p[:3]} {p[3:]}", p, f"{p[:3]} {p[3:]}".lower()])


def messy_amount(x: float) -> str:
    style = random.choice(["plain", "plain", "dollar", "trim"])
    if style == "dollar":
        return f"${x:,.2f}"
    if style == "trim":
        return f"{x:.2f}".rstrip("0").rstrip(".")
    return f"{x:.2f}"


def messy_name(name: str) -> str:
    return random.choice([name, name, name, name.upper(), f"  {name} ", name.lower()])


# ---------- customers ----------
def build_customers():
    customers = []
    for i in range(1, N_CUSTOMERS + 1):
        city = random.choice(list(CITIES))
        prov = CITIES[city]
        opened = date(2005, 1, 1) + timedelta(days=random.randint(0, 365 * 20))
        customers.append({
            "CUST_ID": f"C{i:06d}",
            "ACCT_NO": f"WA-{100000 + i}",
            "FULL_NAME": messy_name(fake.name()),
            "PHONE": messy_phone(prov),
            "EMAIL": fake.email() if random.random() > 0.15 else "",
            "SVC_ADDR": fake.street_address(),
            "CITY": city,
            "PROV": random.choice(PROV_VARIANTS[prov]),
            "POSTAL": messy_postal(),
            "STATUS": random.choice(STATUS_VARIANTS),
            "OPEN_DT": messy_date(opened),
        })
    df = pd.DataFrame(customers)

    # Plant blank account numbers - only on customers who will have NO meters,
    # so rejecting them doesn't cascade into orphaned meters (keeps counts exact).
    blank_idx = random.sample(range(N_CUSTOMERS - 300, N_CUSTOMERS), N_BLANK_ACCT)
    df.loc[blank_idx, "ACCT_NO"] = random.choices(["", " ", None], k=N_BLANK_ACCT)

    # Plant duplicates: copy rows (same CUST_ID + ACCT_NO) with a different name
    # casing / spacing and a different phone format. Never copy a blank-acct row.
    dup_pool = [i for i in range(N_CUSTOMERS) if i not in set(blank_idx)]
    dup_idx = random.sample(dup_pool, N_DUP_CUSTOMERS)
    dups = df.loc[dup_idx].copy()
    dups["FULL_NAME"] = dups["FULL_NAME"].apply(lambda n: messy_name(str(n).strip().title()))
    dups["PHONE"] = dups["CITY"].apply(lambda c: messy_phone(CITIES[c]))
    df = pd.concat([df, dups], ignore_index=True).sample(frac=1, random_state=SEED).reset_index(drop=True)
    return df, set(blank_idx)


# ---------- meters ----------
def build_meters(customers_df, blank_idx):
    # Customers allowed to own meters: everyone except the "no-meter" tail (last 300 rows of the base set)
    base_ids = [f"C{i:06d}" for i in range(1, N_CUSTOMERS - 300 + 1)]
    meters = []
    for i in range(1, N_METERS + 1):
        mtype = random.choice(list(METER_TYPE_VARIANTS))
        installed = date(2005, 1, 1) + timedelta(days=random.randint(0, 365 * 20))
        meters.append({
            "METER_ID": f"M{i:06d}",
            "CUST_ID": random.choice(base_ids),
            "METER_TYPE": random.choice(METER_TYPE_VARIANTS[mtype]),
            "_canon_type": mtype,  # internal helper, dropped before saving
            "INSTALL_DT": messy_date(installed),
            "STATUS": random.choice(["A", "ACTIVE", "Active", "I", "INACTIVE", "RETIRED"]),
        })
    df = pd.DataFrame(meters)
    orphan_idx = random.sample(range(N_METERS), N_ORPHAN_METERS)
    df.loc[orphan_idx, "CUST_ID"] = [f"C9{random.randint(0, 99999):05d}" for _ in orphan_idx]
    return df, set(orphan_idx)


# ---------- bills ----------
def build_bills(meters_df, orphan_meter_idx):
    valid = meters_df.drop(index=list(orphan_meter_idx)).reset_index(drop=True)
    bills = []
    for i in range(1, N_BILLS + 1):
        m = valid.iloc[random.randrange(len(valid))]
        mtype = m["_canon_type"]
        per_unit, fixed = RATES[mtype]
        usage = round(random.uniform(5, 40) * (1000 if mtype == "WATER" else 30 if mtype == "ELECTRIC" else 10), 1)
        amount = round(usage * per_unit + fixed, 2)
        start = date(2023, 1, 1) + timedelta(days=30 * random.randint(0, 35))
        end = start + timedelta(days=29)
        paid = amount if random.random() > 0.12 else (round(amount * random.uniform(0, 0.9), 2) if random.random() > 0.4 else None)
        bills.append({
            "BILL_ID": f"B{i:07d}",
            "METER_ID": m["METER_ID"],
            "PERIOD_START": messy_date(start),
            "PERIOD_END": messy_date(end),
            "BILL_DT": messy_date(end + timedelta(days=3)),
            "USAGE": usage,
            "AMT_BILLED": messy_amount(amount),
            "AMT_PAID": "" if paid is None else messy_amount(paid),
        })
    df = pd.DataFrame(bills)

    # Plant orphan bills (meter doesn't exist) and null-amount bills, on disjoint rows
    all_idx = list(range(N_BILLS))
    orphan_bill_idx = set(random.sample(all_idx, N_ORPHAN_BILLS))
    remaining = [i for i in all_idx if i not in orphan_bill_idx]
    null_idx = set(random.sample(remaining, N_NULL_AMOUNT_BILLS))
    df.loc[list(orphan_bill_idx), "METER_ID"] = [f"M9{random.randint(0, 99999):05d}" for _ in orphan_bill_idx]
    df.loc[list(null_idx), "AMT_BILLED"] = random.choices(["", None], k=len(null_idx))
    return df, orphan_bill_idx, null_idx


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    customers, blank_idx = build_customers()
    meters, orphan_meter_idx = build_meters(customers, blank_idx)
    bills, orphan_bill_idx, null_idx = build_bills(meters, orphan_meter_idx)

    meters_out = meters.drop(columns=["_canon_type"])

    customers.to_csv(OUT_DIR / "customers.csv", index=False)
    meters_out.to_csv(OUT_DIR / "meters.csv", index=False)
    bills.to_csv(OUT_DIR / "bills.csv", index=False)

    # Expected clean-load numbers if the pipeline catches every planted defect
    key = {
        "customers": {
            "rows_in_file": len(customers),
            "duplicate_rows_planted": N_DUP_CUSTOMERS,
            "blank_account_number_planted": N_BLANK_ACCT,
            "expected_loaded": N_CUSTOMERS - N_BLANK_ACCT,
        },
        "meters": {
            "rows_in_file": len(meters_out),
            "orphan_customer_ref_planted": N_ORPHAN_METERS,
            "expected_loaded": N_METERS - N_ORPHAN_METERS,
        },
        "bills": {
            "rows_in_file": len(bills),
            "orphan_meter_ref_planted": N_ORPHAN_BILLS,
            "missing_amount_planted": N_NULL_AMOUNT_BILLS,
            "expected_loaded": N_BILLS - N_ORPHAN_BILLS - N_NULL_AMOUNT_BILLS,
        },
        "other_defects_to_clean_not_reject": [
            "3 date formats (YYYY-MM-DD, MM/DD/YYYY, DD-Mon-YYYY)",
            "5 phone formats",
            "province spelled many ways",
            "status codes A/ACTIVE/Active/I/INACTIVE/C/CLOSED/RETIRED",
            "meter types WATER/WTR/water/ELEC/ELECTRIC/GAS...",
            "money as '$1,204.50', '1204.5', '1204.50'",
            "names in UPPER/lower case or with extra spaces",
            "postal codes with/without space, lower case",
        ],
    }
    (OUT_DIR / "_answer_key.json").write_text(json.dumps(key, indent=2))

    print(f"customers.csv: {len(customers):>6} rows")
    print(f"meters.csv:    {len(meters_out):>6} rows")
    print(f"bills.csv:     {len(bills):>6} rows")
    print(f"Written to {OUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
