import Foundation
import SQLite3

/// Persistent on-device checkpoint: a photo resource is marked complete ONLY after Windows
/// acknowledges final whole-file SHA-256. Reinstalling does not use iCloud or remote state.
final class CheckpointStore {
    private var database: OpaquePointer?
    private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    init() throws {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        let path = base.appendingPathComponent("sync-checkpoints.sqlite").path
        guard sqlite3_open(path, &database) == SQLITE_OK else {
            throw NSError(domain: "CheckpointStore", code: 1)
        }
        let sql = "CREATE TABLE IF NOT EXISTS sent (resource_key TEXT PRIMARY KEY, confirmed_at TEXT NOT NULL);"
        guard sqlite3_exec(database, sql, nil, nil, nil) == SQLITE_OK else {
            throw NSError(domain: "CheckpointStore", code: 2)
        }
    }

    deinit { sqlite3_close(database) }

    func contains(_ key: String) -> Bool {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, "SELECT 1 FROM sent WHERE resource_key=?", -1, &statement, nil) == SQLITE_OK
        else { return false }
        defer { sqlite3_finalize(statement) }
        sqlite3_bind_text(statement, 1, (key as NSString).utf8String, -1, transient)
        return sqlite3_step(statement) == SQLITE_ROW
    }

    func markCompleted(_ key: String) throws {
        var statement: OpaquePointer?
        let sql = "INSERT OR REPLACE INTO sent(resource_key,confirmed_at) VALUES(?,datetime('now'))"
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK else {
            throw NSError(domain: "CheckpointStore", code: 3)
        }
        defer { sqlite3_finalize(statement) }
        sqlite3_bind_text(statement, 1, (key as NSString).utf8String, -1, transient)
        guard sqlite3_step(statement) == SQLITE_DONE else {
            throw NSError(domain: "CheckpointStore", code: 4)
        }
    }
}
