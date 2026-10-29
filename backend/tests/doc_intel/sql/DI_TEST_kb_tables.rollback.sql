-- TEST FIXTURE ONLY — removes the isolated KB table copies (chunk table first: FK).
-- PURGE: these hold throwaway test rows only; no reason to keep them in the recycle bin.

DROP TABLE DI_TEST_KB_CHUNK PURGE;

DROP TABLE DI_TEST_KB_CORPUS PURGE;
