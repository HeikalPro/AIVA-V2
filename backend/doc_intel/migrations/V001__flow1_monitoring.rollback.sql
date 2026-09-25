-- Document intelligence — rollback of migration V001
--
-- Drops ONLY the tables created by V001 (their indexes, constraints and identity
-- sequences go with them). Tables go to the recycle bin (no PURGE), so a mistaken
-- rollback can be undone with:  FLASHBACK TABLE <name> TO BEFORE DROP;
--
-- The runner refuses to execute this while any document is still PUBLISHED:
-- unpublish first, otherwise kbdoc-* verticals and chunks would stay live in the KB
-- with no registry row pointing at them.

DROP TABLE AIVA_health_check_events;

DROP TABLE AIVA_health_checks;

DROP TABLE AIVA_kb_documents;

DROP TABLE AIVA_di_schema_version;
