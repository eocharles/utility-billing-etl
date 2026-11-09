-- Run this once in MySQL Workbench (File > New Query Tab > paste > lightning bolt).
-- Creates an empty target database and a dedicated user for the ETL script.
-- Change the password before you run it, and don't commit the real one to GitHub.

CREATE DATABASE IF NOT EXISTS utility_billing
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE USER IF NOT EXISTS 'etl_user'@'localhost' IDENTIFIED BY 'CHANGE_ME_please';
GRANT ALL PRIVILEGES ON utility_billing.* TO 'etl_user'@'localhost';
FLUSH PRIVILEGES;

-- Sanity check:
SHOW DATABASES LIKE 'utility_billing';
