-- Reset the shop database to the exact state in EVALS §2.
--
-- Run with ON_ERROR_STOP and --single-transaction (./run.sh reset does both), so a
-- failure anywhere rolls the whole reset back instead of leaving half-seeded tables.
-- Tables are dropped and recreated, so every SERIAL restarts at 1 and order ids
-- match the EVALS §2 table.
--
-- Expects one psql variable: toolbox_pw (the least-privilege role's password).

SET client_min_messages = warning;   -- hide "does not exist, skipping" on first run

-- ── least-privilege role for the MCP Toolbox ──────────────────────────────────
SELECT format('CREATE ROLE toolbox LOGIN PASSWORD %L', :'toolbox_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'toolbox')
\gexec
ALTER ROLE toolbox WITH LOGIN PASSWORD :'toolbox_pw';

-- ── drop everything this script owns ─────────────────────────────────────────
DROP TABLE IF EXISTS actions_log;
DROP TABLE IF EXISTS customer_orders;
DROP TABLE IF EXISTS users;

-- ── users ─────────────────────────────────────────────────────────────────────
CREATE TABLE users (
    user_id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    full_name VARCHAR(100),
    is_premium_customer BOOLEAN DEFAULT FALSE,
    total_items_purchased INTEGER DEFAULT 0,
    password VARCHAR(255) NOT NULL          -- demo only; see SPEC §8
);

INSERT INTO users (email, full_name, is_premium_customer, total_items_purchased, password) VALUES
    ('hannah.m@school.edu',     'Hannah M',     TRUE,  94, 'hannah'),
    ('charlie.d@webmail.com',   'Charlie D',    TRUE,  88, 'charlie'),
    ('julia.child@kitchen.com', 'Julia Child',  TRUE,  75, 'julia'),
    ('evan.g@bizcorp.com',      'Evan G',       TRUE,  56, 'evan'),
    ('alice.jones@example.com', 'Alice Jones',  FALSE, 42, 'alice'),
    ('ian.malcolm@chaos.com',   'Ian Malcolm',  FALSE, 31, 'ian'),
    ('diana.prince@hero.net',   'Diana Prince', FALSE, 23, 'diana'),
    ('george.j@jungle.com',     'George J',     FALSE, 19, 'george'),
    ('bob.smith@techmail.com',  'Bob Smith',    FALSE, 15, 'bob'),
    ('fiona.shrek@swamp.com',   'Fiona Shrek',  FALSE, 12, 'fiona');

-- ── customer_orders ───────────────────────────────────────────────────────────
CREATE TABLE customer_orders (
    order_id SERIAL PRIMARY KEY,
    customer_email VARCHAR(100) NOT NULL,
    delivery_address VARCHAR(255),
    status VARCHAR(20) CHECK (status IN ('PROCESSING','SHIPPED','DELIVERED','CANCELLED','RETURNED')),
    items JSONB,
    order_date TIMESTAMPTZ DEFAULT NOW(),
    total_amount DECIMAL(10,2)
);

-- every read tool filters on customer_email (M-4)
CREATE INDEX customer_orders_customer_email_idx ON customer_orders (customer_email);

-- insertion order fixes the order ids; do not reorder (EVALS §2)
INSERT INTO customer_orders (customer_email, delivery_address, status, items, order_date, total_amount) VALUES
('alice.jones@example.com','123 Market St, Springfield','DELIVERED','[{"product":"Ergonomic Office Chair","qty":1,"price":250.00}]',NOW()-INTERVAL '6 months',250.00),
('alice.jones@example.com','123 Market St, Springfield','DELIVERED','[{"product":"Wireless Mouse","qty":1,"price":25.00}]',NOW()-INTERVAL '3 months',25.00),
('alice.jones@example.com','123 Market St, Springfield','SHIPPED','[{"product":"Mechanical Keyboard","qty":1,"price":120.00}]',NOW()-INTERVAL '2 days',120.00),
('alice.jones@example.com','123 Market St, Springfield','PROCESSING','[{"product":"USB-C Hub","qty":1,"price":45.00}]',NOW()-INTERVAL '1 hour',45.00),
('bob.smith@techmail.com','88 Tech Ave, Seattle','DELIVERED','[{"product":"Gaming Laptop 15-inch","qty":1,"price":1500.00}]',NOW()-INTERVAL '1 year',1500.00),
('bob.smith@techmail.com','88 Tech Ave, Seattle','CANCELLED','[{"product":"VR Headset","qty":1,"price":400.00}]',NOW()-INTERVAL '10 days',400.00),
('bob.smith@techmail.com','88 Tech Ave, Seattle','PROCESSING','[{"product":"Curved Monitor 34-inch","qty":1,"price":450.00}]',NOW()-INTERVAL '4 hours',450.00),
('charlie.d@webmail.com','12 Oak Rd, Denver','DELIVERED','[{"product":"AA Batteries (Pack of 12)","qty":2,"price":15.00}]',NOW()-INTERVAL '45 days',30.00),
('diana.prince@hero.net','5 Hero Ln, Metropolis','DELIVERED','[{"product":"Smart Watch Gen 5","qty":1,"price":299.00}]',NOW()-INTERVAL '60 days',299.00),
('diana.prince@hero.net','5 Hero Ln, Metropolis','RETURNED','[{"product":"Running Shoes","qty":1,"price":120.00}]',NOW()-INTERVAL '15 days',120.00),
('evan.g@bizcorp.com','200 Business Pkwy, Austin','SHIPPED','[{"product":"Office Desk","qty":2,"price":300.00}]',NOW()-INTERVAL '1 day',600.00),
('fiona.shrek@swamp.com','7 Swamp Rd, Bayou','CANCELLED','[{"product":"Skincare Gift Set","qty":1,"price":85.00}]',NOW()-INTERVAL '5 days',85.00),
('george.j@jungle.com','9 Jungle Path, Amazonia','PROCESSING','[{"product":"Bluetooth Speaker","qty":1,"price":60.00}]',NOW()-INTERVAL '30 minutes',60.00),
('hannah.m@school.edu','4 Campus Dr, Boston','DELIVERED','[{"product":"Notebook Pack","qty":5,"price":12.00}]',NOW()-INTERVAL '4 months',60.00),
('ian.malcolm@chaos.com','22 Chaos Blvd, San Diego','DELIVERED','[{"product":"Professional Camera Lens","qty":1,"price":2200.00}]',NOW()-INTERVAL '8 months',2200.00),
('julia.child@kitchen.com','10 Kitchen St, Portland','DELIVERED','[{"product":"Coffee Beans 1kg","qty":1,"price":25.00}]',NOW()-INTERVAL '3 months',25.00),
('julia.child@kitchen.com','10 Kitchen St, Portland','PROCESSING','[{"product":"Descaling Kit","qty":1,"price":15.00}]',NOW()-INTERVAL '3 hours',15.00);

-- ── actions_log ───────────────────────────────────────────────────────────────
CREATE TABLE actions_log (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    user_email VARCHAR(255) NOT NULL,
    action_type VARCHAR(50) NOT NULL
        CONSTRAINT actions_log_action_type_check   -- the enum lives in the schema (M-7, R-11)
        CHECK (action_type IN ('CANCEL_ORDER','RETURN_ORDER','UPDATE_ADDRESS','UPDATE_PREFERENCE','UPDATE_PROFILE')),
    parameters JSONB NOT NULL
);

-- ── grants: the Toolbox may read, and may only append to actions_log ──────────
-- No UPDATE/DELETE on customer_orders at the database level either, so even a
-- mutating tool declared by mistake would fail (defence in depth for M-6).
GRANT CONNECT ON DATABASE :"DBNAME" TO toolbox;
GRANT USAGE ON SCHEMA public TO toolbox;
GRANT SELECT ON users, customer_orders TO toolbox;
GRANT SELECT, INSERT ON actions_log TO toolbox;
GRANT USAGE ON SEQUENCE actions_log_id_seq TO toolbox;
