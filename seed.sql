BEGIN;
CREATE TABLE IF NOT EXISTS price_history (
    id          SERIAL PRIMARY KEY,
    trip_id     TEXT NOT NULL,
    scraped_at  TIMESTAMP NOT NULL,
    price_usd   DOUBLE PRECISION,
    airline     TEXT,
    stops       TEXT,
    depart_time TEXT,
    arrive_time TEXT,
    notes       TEXT,
    flights     TEXT
);
CREATE INDEX IF NOT EXISTS price_history_trip_time ON price_history (trip_id, scraped_at);
TRUNCATE price_history RESTART IDENTITY;
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-02T08:00:00.000000', 1249.0, 'Delta', NULL, '6:00 am', '2:51 pm', 'Main Classic', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-03T08:00:00.000000', 1249.0, 'Delta', NULL, '6:00 am', '2:51 pm', 'Main Classic', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-04T14:58:41.893487', 1079.0, 'Delta', NULL, '6:00 am', '2:51 pm', 'Main', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-06T02:14:44.865910', 1541.0, 'Delta', NULL, '6:00 am', '2:51 pm', 'Main', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-06T17:23:17.200728', 1425.0, 'Delta', NULL, '6:00 am', '2:51 pm', 'Main', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-07T18:59:51.764699', 1425.0, 'Delta', NULL, '6:00 am', '2:51 pm', 'Main', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-07T21:33:08.698300', 1425.0, 'Delta', NULL, '6:00 am', '2:51 pm', 'Main', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-08T10:00:49.894981', 1123.0, 'Delta', NULL, '6:00 am', '2:51 pm', 'Main', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-08T13:56:33.915848', 1293.0, 'Delta', '1', '06:00', '14:51', 'Main Classic', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-09T10:01:18.705921', 1409.0, 'Delta', '1', '06:00', '14:51', 'Main Classic', 'DL2453 / DL1765');
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-10T01:01:20.147179', 1409.0, 'Delta', '1', '06:00', '14:51', 'Main Classic', 'DL2453 / DL1765');
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-bon-2026-11-28', '2026-09-10T10:01:22.681216', 1541.0, 'Delta', '1', '06:00', '14:51', 'Main Classic', 'DL2453 / DL1765');
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-lga-2026-09-29', '2026-09-07T21:49:27.249185', 687.0, 'Delta', 'Nonstop', '6:00 am', '7:58 am', 'Main', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-lga-2026-09-29', '2026-09-08T01:00:52.305883', 687.0, 'Delta', 'Nonstop', '6:00 am', '7:58 am', 'Main', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-lga-2026-09-29', '2026-09-08T10:02:07.453728', 687.0, 'Delta', 'Nonstop', '6:00 am', '7:58 am', 'Main', NULL);
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-lga-2026-09-29', '2026-09-09T10:02:37.642105', 717.0, 'Delta', '0', '06:00', '07:58', 'Main Classic', 'DL5760');
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-lga-2026-09-29', '2026-09-10T01:02:33.093215', 717.0, 'Delta', '0', '06:00', '07:58', 'Main Classic', 'DL5760');
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-lga-2026-09-29', '2026-09-10T10:02:49.972994', 717.0, 'Delta', '0', '06:00', '07:58', 'Main Classic', 'DL5760');
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-fco-2027-01-15', '2026-09-08T14:51:40.090542', 1002.0, 'Delta', '1', '13:00', '11:20', 'Main Classic', 'DL2547 / DL66');
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-fco-2027-01-15', '2026-09-08T16:25:52.794024', 1002.0, 'Delta', '1', '13:00', '11:20', 'Main Classic', 'DL2547 / DL66');
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-fco-2027-01-15', '2026-09-09T10:03:50.031452', 852.0, 'Delta', '1', '13:00', '11:20', 'Main Classic', 'DL2547 / DL66');
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-fco-2027-01-15', '2026-09-10T01:03:37.607277', 852.0, 'Delta', '1', '13:00', '11:20', 'Main Classic', 'DL2547 / DL66');
INSERT INTO price_history (trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) VALUES ('grr-fco-2027-01-15', '2026-09-10T10:04:00.128348', 852.0, 'Delta', '1', '13:00', '11:20', 'Main Classic', 'DL2547 / DL66');
COMMIT;
