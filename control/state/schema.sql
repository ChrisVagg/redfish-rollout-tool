-- The rollout state database: every event of every run, and a lock per host. PostgreSQL runs this file on its first
-- start, into an empty volume, as the owner rollout. Three accounts: rollout, the owner (make state and make unlock);
-- redfish_rollout_sa, rollout.py run's; grafana, Grafana's. Their passwords come from control/state/.state.env.
\getenv rollout_sa_password REDFISH_ROLLOUT_SA_PASSWORD
\getenv grafana_password GRAFANA_PASSWORD

-- Every event of every run, as the run record (<site>/runs/<pipeline>-<stage>.jsonl) has it, the whole of it in event:
-- append-only, as redfish_rollout_sa may only add rows. Kept until deleted: PostgreSQL has no retention of its own
CREATE TABLE events (
  id       bigserial PRIMARY KEY,
  at       timestamptz NOT NULL,
  site     text NOT NULL,
  pipeline text NOT NULL,
  stage    text NOT NULL,
  wave     text,
  host     text NOT NULL,  -- '-' for the run's own events: its start, gates and end
  step     text NOT NULL,
  state    text NOT NULL,
  event    jsonb NOT NULL
);
CREATE INDEX ON events (host, at);
CREATE INDEX ON events (at);

-- Where each host of a pipeline is: its latest event
CREATE VIEW host_state AS
  SELECT DISTINCT ON (pipeline, host) site, pipeline, host, stage, wave, step, state, event->>'detail' AS detail, at
  FROM events WHERE host <> '-' ORDER BY pipeline, host, id DESC;

-- The one pipeline that may touch a host. A run takes the locks of every host of its stage before it starts, or none,
-- and gives them back at its end; a host left for a person, or every host of a run that stopped halfway, keeps its
-- lock until make unlock: a lock never expires
CREATE TABLE host_locks (
  host     text PRIMARY KEY,
  site     text NOT NULL,
  pipeline text NOT NULL,
  stage    text NOT NULL,
  since    timestamptz NOT NULL DEFAULT now()
);

-- rollout.py run: adds events, never changes or deletes one; takes and gives back locks
CREATE ROLE redfish_rollout_sa LOGIN PASSWORD :'rollout_sa_password';
GRANT SELECT, INSERT ON events TO redfish_rollout_sa;
GRANT USAGE ON SEQUENCE events_id_seq TO redfish_rollout_sa;
GRANT SELECT ON host_state TO redfish_rollout_sa;
GRANT SELECT, INSERT, DELETE ON host_locks TO redfish_rollout_sa;

-- Grafana: reads only
CREATE ROLE grafana LOGIN PASSWORD :'grafana_password';
GRANT SELECT ON events, host_state, host_locks TO grafana;
