SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS revision_details;
DROP TABLE IF EXISTS revisions;
DROP TABLE IF EXISTS meter_events;
DROP TABLE IF EXISTS connectors;
DROP TABLE IF EXISTS inbox;
DROP TABLE IF EXISTS charge_points;
SET FOREIGN_KEY_CHECKS = 1;

-- A charge point is one physical unit on a site. It speaks for itself and,
-- on multi-connector hardware, for each of its connectors.
CREATE TABLE charge_points (
  id               INT PRIMARY KEY,
  cp_ident         VARCHAR(32) NOT NULL UNIQUE,
  wh               INT              DEFAULT 0,
  connector_count  INT              DEFAULT 0,
  model_code       TINYINT UNSIGNED DEFAULT 0,
  flags            TINYINT          DEFAULT 0,
  link_state       TINYINT          DEFAULT 0,
  via_gateway      TINYINT          DEFAULT 0,
  tz               VARCHAR(48)      DEFAULT 'America/Chicago',
  tariff           VARCHAR(16)      NULL,
  firmware         VARCHAR(24)      NULL,
  last_seen_at     DATETIME         NULL,
  local_event_at   DATETIME         NULL,
  rollup_at        DATETIME         NULL,
  rollup_event_id  BIGINT           NULL,
  last_event_id    BIGINT           NULL,
  fault_note       VARCHAR(255)     NULL,
  alerted_at       DATETIME         NULL,
  settled          TINYINT          DEFAULT 0
) ENGINE=InnoDB;

-- Raw frames off the wire, before anyone has decided what they mean.
CREATE TABLE inbox (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  src         VARCHAR(16) NOT NULL,
  status      VARCHAR(16) NOT NULL DEFAULT 'new',
  cp_ident    VARCHAR(32) NOT NULL,
  body        TEXT        NOT NULL,
  body_hash   CHAR(40)    NOT NULL,
  received_at DATETIME    NOT NULL,
  KEY k_status (status, id)
) ENGINE=InnoDB;

CREATE TABLE meter_events (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  inbox_id       BIGINT NOT NULL,
  cp_id          INT    NOT NULL,
  msg_type       TINYINT      NOT NULL,
  wh             INT          NULL,
  raw            VARCHAR(255) NULL,
  local_event_at DATETIME     NULL,
  utc_event_at   DATETIME     NULL,
  rollup_date    DATE         NULL,
  rollup_hour    TINYINT      NULL,
  fault_note     VARCHAR(255) NULL,
  flags          TINYINT      DEFAULT 0,
  CONSTRAINT fk_ev_cp    FOREIGN KEY (cp_id)    REFERENCES charge_points(id),
  CONSTRAINT fk_ev_inbox FOREIGN KEY (inbox_id) REFERENCES inbox(id)
) ENGINE=InnoDB;

CREATE TABLE connectors (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  cp_id           INT NOT NULL,
  connector_ident VARCHAR(32) NOT NULL,
  connector_no    INT NOT NULL,
  panel           INT NULL,
  retired         TINYINT DEFAULT 0,
  CONSTRAINT fk_conn_cp FOREIGN KEY (cp_id) REFERENCES charge_points(id)
) ENGINE=InnoDB;

-- Change header. Polymorphic target, so no foreign key is possible.
CREATE TABLE revisions (
  id        BIGINT AUTO_INCREMENT PRIMARY KEY,
  who       INT DEFAULT 0,
  tname     VARCHAR(32) NOT NULL,
  target_id VARCHAR(32) NOT NULL,
  op        CHAR(1) NOT NULL,
  at_       DATETIME NOT NULL,
  KEY k_t (tname, target_id)
) ENGINE=InnoDB;

CREATE TABLE revision_details (
  id      BIGINT AUTO_INCREMENT PRIMARY KEY,
  rev_id  BIGINT NOT NULL,
  col     VARCHAR(48) NOT NULL,
  before_ VARCHAR(255) NULL,
  after_  VARCHAR(255) NULL,
  CONSTRAINT fk_rdt_rev FOREIGN KEY (rev_id) REFERENCES revisions(id)
) ENGINE=InnoDB;

-- model_code 0 = single connector, 7 = multi-connector, 253 = legacy vendor build
INSERT INTO charge_points (id, cp_ident, wh, connector_count, model_code, flags, link_state, via_gateway, tz) VALUES
 (1, 'CP-0001', 0, 1, 0,   0, 0, 1, 'America/Chicago'),
 (2, 'CP-0002', 0, 3, 7,   0, 0, 1, 'America/Chicago'),
 (3, 'CP-0003', 0, 1, 0,   0, 0, 0, 'America/Denver'),
 (4, 'CP-0004', 0, 2, 253, 0, 0, 1, 'America/Chicago');

INSERT INTO charge_points (id, cp_ident, wh, connector_count, model_code, flags, link_state, via_gateway, tz)
SELECT n, CONCAT('CP-', LPAD(n, 4, '0')), 0, 1, 0, 0, 0, 1, 'America/Chicago'
FROM (
  SELECT 10 + (a.i + b.i * 10) AS n
  FROM (SELECT 0 i UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4
        UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) a
  CROSS JOIN (SELECT 0 i UNION SELECT 1 UNION SELECT 2) b
) g
WHERE n <= 21;

INSERT INTO connectors (cp_id, connector_ident, connector_no, panel, retired) VALUES
 (2, 'CP-0002-1', 1, 0, 0),
 (2, 'CP-0002-2', 2, 0, 0),
 (2, 'CP-0002-3', 3, 1, 0);
