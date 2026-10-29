-- Document intelligence — rollback of migration V002
--
-- Drops ONLY the tables created by V002, children first (their indexes, constraints and
-- identity sequences go with them). Tables go to the recycle bin (no PURGE), so a mistaken
-- rollback can be undone with:  FLASHBACK TABLE <name> TO BEFORE DROP;
-- The runner also removes the '002' row from AIVA_di_schema_version.
-- Stored Microsoft credentials and extracted CRM entities are lost with these tables.

DROP TABLE AIVA_crm_entities;

DROP TABLE AIVA_crm_source_files;

DROP TABLE AIVA_crm_sync_runs;

DROP TABLE AIVA_crm_sources;
