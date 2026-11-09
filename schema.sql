-- schema.sql : target schema for the utility_billing database (MySQL 8)
--
-- Q1. Why DECIMAL and not FLOAT for money?
--     FLOAT is approximate (stored in binary), DECIMAL is exact. Money must be exact,
--     or totals stop reconciling to the cent. DECIMAL(10,2) also fixes two decimal places.
-- Q2. Why create customers, then meters, then bills (and drop them in reverse)?
--     meters has a foreign key to customers, and bills has one to meters. A parent table
--     must exist before its children can reference it, and it cannot be dropped while
--     a child still points at it.
-- Q3. Why is amt_paid NOT NULL DEFAULT 0.00?
--     The source has blank AMT_PAID on 500 bills. I treat blank as "nothing paid yet" (0.00)
--     because amt_billed - amt_paid returns NULL when amt_paid is NULL, which would silently
--     drop unpaid bills from any balance-owed query.
--     ASSUMPTION to confirm with the client: blank means unpaid, not "unknown".

USE utility_billing;

DROP TABLE IF EXISTS bills;
DROP TABLE IF EXISTS meters;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    cust_id       VARCHAR(10)  NOT NULL,
    acct_no       VARCHAR(12)  NOT NULL,                  -- required; the legacy file had blanks
    full_name     VARCHAR(100) NOT NULL,
    phone         CHAR(10)     NULL,                      -- digits only, e.g. 9025550123
    email         VARCHAR(120) NULL,                      -- ~15% blank in the source
    service_addr  VARCHAR(150) NOT NULL,
    city          VARCHAR(50)  NOT NULL,
    province      CHAR(2)      NOT NULL,                  -- PE / NS / NB
    postal_code   CHAR(6)      NOT NULL,                  -- upper case, no space
    status        ENUM('ACTIVE', 'INACTIVE', 'CLOSED') NOT NULL,
    open_date     DATE         NOT NULL,
    PRIMARY KEY (cust_id),
    UNIQUE KEY uq_customers_acct_no (acct_no)             -- no two customers share an account number
) ENGINE = InnoDB;

CREATE TABLE meters (
    meter_id      VARCHAR(10)  NOT NULL,
    cust_id       VARCHAR(10)  NOT NULL,
    meter_type    ENUM('WATER', 'ELECTRIC', 'GAS') NOT NULL,
    install_date  DATE         NOT NULL,
    status        ENUM('ACTIVE', 'INACTIVE', 'RETIRED') NOT NULL,   -- meters can be RETIRED
    PRIMARY KEY (meter_id),
    CONSTRAINT fk_meters_customer
        FOREIGN KEY (cust_id) REFERENCES customers (cust_id)
) ENGINE = InnoDB;

CREATE TABLE bills (
    bill_id       VARCHAR(10)   NOT NULL,
    meter_id      VARCHAR(10)   NOT NULL,
    period_start  DATE          NOT NULL,
    period_end    DATE          NOT NULL,
    bill_date     DATE          NOT NULL,
    usage_amount  DECIMAL(12,1) NOT NULL,                 -- source has 1 decimal place
    amt_billed    DECIMAL(10,2) NOT NULL,                 -- money: exact, 2 decimal places
    amt_paid      DECIMAL(10,2) NOT NULL DEFAULT 0.00,    -- blank in source = unpaid (see Q3)
    PRIMARY KEY (bill_id),
    CONSTRAINT fk_bills_meter
        FOREIGN KEY (meter_id) REFERENCES meters (meter_id),
    CONSTRAINT chk_bills_period CHECK (period_end >= period_start)
) ENGINE = InnoDB;

-- Sanity check after running: should list customers, meters, bills
SHOW TABLES;
