import Foundation
import SQLite3

struct ResourceCheckpoint {
    let sha256: String
    let size: Int64
}

/// Legacy sent rows are retained but never trusted without receiver reconciliation.
final class CheckpointStore {
    private var database: OpaquePointer?
    private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    init() throws {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        guard sqlite3_open(base.appendingPathComponent("sync-checkpoints.sqlite").path, &database) == SQLITE_OK else {
            sqlite3_close(database)
            database = nil
            throw NSError(domain: "CheckpointStore", code: 1)
        }
        var initialized = false
        defer { if !initialized { sqlite3_close(database); database = nil } }
        let sql = """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS confirmed_resources_v2 (
          receiver_id TEXT NOT NULL, resource_key TEXT NOT NULL,
          version_key TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
          PRIMARY KEY(receiver_id,resource_key));
        CREATE TABLE IF NOT EXISTS failed_resources (
          receiver_id TEXT NOT NULL, resource_key TEXT NOT NULL,
          message TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(receiver_id,resource_key));
        """
        guard sqlite3_exec(database, sql, nil, nil, nil) == SQLITE_OK else {
            throw NSError(domain: "CheckpointStore", code: 2)
        }
        initialized = true
    }
    deinit { sqlite3_close(database) }

    private func statement(_ sql: String, values: [String]) throws -> OpaquePointer {
        var pointer: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &pointer, nil) == SQLITE_OK, let pointer else {
            throw NSError(domain: "CheckpointStore", code: 3)
        }
        for (index, value) in values.enumerated() {
            sqlite3_bind_text(pointer, Int32(index + 1), (value as NSString).utf8String, -1, transient)
        }
        return pointer
    }

    func completed(_ key: String, receiver: String, version: String) throws -> ResourceCheckpoint? {
        let query = try statement("SELECT sha256,size FROM confirmed_resources_v2 WHERE receiver_id=? AND resource_key=? AND version_key=?", values: [receiver, key, version])
        defer { sqlite3_finalize(query) }
        let result = sqlite3_step(query)
        if result == SQLITE_DONE { return nil }
        guard result == SQLITE_ROW, let digest = sqlite3_column_text(query, 0) else { throw NSError(domain: "CheckpointStore", code: 4) }
        return ResourceCheckpoint(sha256: String(cString: digest), size: sqlite3_column_int64(query, 1))
    }

    func markCompleted(_ key: String, receiver: String, version: String, exported: ExportedPhotoResource) throws {
        let query = try statement("INSERT OR REPLACE INTO confirmed_resources_v2 VALUES(?,?,?,?,?)", values: [receiver, key, version, exported.sha256])
        defer { sqlite3_finalize(query) }
        sqlite3_bind_int64(query, 5, exported.size)
        guard sqlite3_step(query) == SQLITE_DONE else { throw NSError(domain: "CheckpointStore", code: 5) }
        try clearFailure(key, receiver: receiver)
    }

    func clearFailure(_ key: String, receiver: String) throws {
        let query = try statement("DELETE FROM failed_resources WHERE receiver_id=? AND resource_key=?", values: [receiver, key])
        defer { sqlite3_finalize(query) }
        guard sqlite3_step(query) == SQLITE_DONE else { throw NSError(domain: "CheckpointStore", code: 6) }
    }

    func recordFailure(_ key: String, receiver: String, message: String) throws {
        let query = try statement("""
          INSERT INTO failed_resources(receiver_id,resource_key,message) VALUES(?,?,?)
          ON CONFLICT(receiver_id,resource_key) DO UPDATE SET message=excluded.message,attempts=attempts+1
          """, values: [receiver, key, String(message.prefix(400))])
        defer { sqlite3_finalize(query) }
        guard sqlite3_step(query) == SQLITE_DONE else { throw NSError(domain: "CheckpointStore", code: 7) }
    }

    func failureCount(receiver: String) throws -> Int {
        let query = try statement("SELECT COUNT(*) FROM failed_resources WHERE receiver_id=?", values: [receiver])
        defer { sqlite3_finalize(query) }
        guard sqlite3_step(query) == SQLITE_ROW else { throw NSError(domain: "CheckpointStore", code: 8) }
        return Int(sqlite3_column_int(query, 0))
    }
}
