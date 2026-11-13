-- Target: each Namespace database (not the shared main database).
-- Records existing before this column is added retain NULL because their last
-- save date cannot be reconstructed from stat_changes.
ALTER TABLE stat_changes
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE NULL;
