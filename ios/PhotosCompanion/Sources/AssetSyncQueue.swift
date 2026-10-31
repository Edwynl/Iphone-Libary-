import Foundation
import SQLite3

struct QueueAsset: Sendable, Equatable {
    let identifier: String
    let version: String
    let changedAt: Double
}

/// Stable identifiers replace the numeric position in a newest-first PhotoKit fetch.
/// Metadata discovery never reads or exports an original. Only a completed snapshot
/// becomes eligible; interrupted discovery is reconciled on the next opportunity.
actor AssetSyncQueue {
    private var database: OpaquePointer?
    private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    static let schema = """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS asset_queue (
      receiver TEXT NOT NULL, asset TEXT NOT NULL, version TEXT NOT NULL,
      changed_at REAL NOT NULL, queued_at REAL NOT NULL, generation TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
      retry_after REAL NOT NULL DEFAULT 0, verified_at REAL NOT NULL DEFAULT 0,
      PRIMARY KEY(receiver,asset));
    CREATE INDEX IF NOT EXISTS asset_queue_jobs ON asset_queue(receiver,generation,state,queued_at DESC,changed_at DESC);
    CREATE TABLE IF NOT EXISTS queue_snapshot(receiver TEXT PRIMARY KEY, generation TEXT NOT NULL);
    """
    static let discoverSQL = """
    INSERT INTO asset_queue(receiver,asset,version,changed_at,queued_at,generation) VALUES(?,?,?,?,?,?)
    ON CONFLICT(receiver,asset) DO UPDATE SET
      state=CASE WHEN asset_queue.version<>excluded.version THEN 'pending' ELSE asset_queue.state END,
      attempts=CASE WHEN asset_queue.version<>excluded.version THEN 0 ELSE asset_queue.attempts END,
      retry_after=CASE WHEN asset_queue.version<>excluded.version THEN 0 ELSE asset_queue.retry_after END,
      queued_at=CASE WHEN asset_queue.version<>excluded.version THEN excluded.queued_at ELSE asset_queue.queued_at END,
      version=excluded.version, changed_at=excluded.changed_at, generation=excluded.generation
    """
    static let pendingSQL = """
    SELECT asset,version,changed_at FROM asset_queue
    WHERE receiver=? AND generation=(SELECT generation FROM queue_snapshot WHERE receiver=?) AND state='pending'
    ORDER BY queued_at DESC,changed_at DESC,asset LIMIT ?
    """
    static let retrySQL = """
    SELECT asset,version,changed_at FROM asset_queue
    WHERE receiver=? AND generation=(SELECT generation FROM queue_snapshot WHERE receiver=?)
      AND state='failed' AND retry_after<=?
    ORDER BY retry_after,asset LIMIT 1
    """
    static let auditSQL = """
    SELECT asset,version,changed_at FROM asset_queue
    WHERE receiver=? AND generation=(SELECT generation FROM queue_snapshot WHERE receiver=?)
      AND state='complete' AND verified_at<?
    ORDER BY verified_at,asset LIMIT 1
    """
    static let completeSQL = """
    UPDATE asset_queue SET state='complete',attempts=0,retry_after=0,verified_at=?
    WHERE receiver=? AND asset=? AND version=?
    """
    static let failureSQL = """
    UPDATE asset_queue SET state='failed',attempts=attempts+1,
      retry_after=?+MIN(21600,60*(1 << MIN(attempts,8)))
    WHERE receiver=? AND asset=? AND version=?
    """
    static let remainingSQL = """
    SELECT COUNT(*) FROM asset_queue WHERE receiver=?
      AND generation=(SELECT generation FROM queue_snapshot WHERE receiver=?) AND state<>'complete'
    """
    static let snapshotSQL = """
    SELECT generation FROM queue_snapshot WHERE receiver=?
    """

    init(url: URL? = nil) throws {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let location = url ?? base.appendingPathComponent("asset-sync-queue.sqlite")
        try FileManager.default.createDirectory(at: location.deletingLastPathComponent(), withIntermediateDirectories: true)
        guard sqlite3_open(location.path, &database) == SQLITE_OK else {
            sqlite3_close(database); database = nil
            throw NSError(domain: "AssetSyncQueue", code: 1)
        }
        guard sqlite3_exec(database, Self.schema, nil, nil, nil) == SQLITE_OK else {
            sqlite3_close(database); database = nil
            throw NSError(domain: "AssetSyncQueue", code: 2)
        }
    }
    deinit { sqlite3_close(database) }

    private func statement(_ sql: String, _ values: [String]) throws -> OpaquePointer {
        var query: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &query, nil) == SQLITE_OK, let query else {
            throw NSError(domain: "AssetSyncQueue", code: 3)
        }
        for (index, value) in values.enumerated() {
            guard sqlite3_bind_text(query, Int32(index + 1), (value as NSString).utf8String, -1, transient) == SQLITE_OK else {
                sqlite3_finalize(query)
                throw NSError(domain: "AssetSyncQueue", code: 4)
            }
        }
        return query
    }
    private func execute(_ sql: String, _ values: [String] = []) throws {
        let query = try statement(sql, values)
        defer { sqlite3_finalize(query) }
        guard sqlite3_step(query) == SQLITE_DONE else { throw NSError(domain: "AssetSyncQueue", code: 5) }
    }
    func discover(_ assets: [QueueAsset], receiver: String, generation: String, now: Double) throws {
        try execute("BEGIN IMMEDIATE")
        do {
            let query = try statement(Self.discoverSQL, [])
            defer { sqlite3_finalize(query) }
            for asset in assets {
                try Task.checkCancellation()
                sqlite3_reset(query)
                let values = [receiver, asset.identifier, asset.version, String(asset.changedAt), String(now), generation]
                for (index, value) in values.enumerated() {
                    guard sqlite3_bind_text(query, Int32(index + 1), (value as NSString).utf8String, -1, transient) == SQLITE_OK else {
                        throw NSError(domain: "AssetSyncQueue", code: 4)
                    }
                }
                guard sqlite3_step(query) == SQLITE_DONE else { throw NSError(domain: "AssetSyncQueue", code: 5) }
            }
            try execute("COMMIT")
        } catch { try? execute("ROLLBACK"); throw error }
    }
    func finishDiscovery(receiver: String, generation: String) throws {
        try execute("INSERT OR REPLACE INTO queue_snapshot VALUES(?,?)", [receiver, generation])
    }
    private func select(_ sql: String, _ values: [String]) throws -> [QueueAsset] {
        let query = try statement(sql, values)
        defer { sqlite3_finalize(query) }
        var result: [QueueAsset] = []
        while true {
            let step = sqlite3_step(query)
            if step == SQLITE_DONE { return result }
            guard step == SQLITE_ROW, let identifier = sqlite3_column_text(query, 0),
                  let version = sqlite3_column_text(query, 1) else { throw NSError(domain: "AssetSyncQueue", code: 6) }
            result.append(QueueAsset(identifier: String(cString: identifier), version: String(cString: version),
                                     changedAt: sqlite3_column_double(query, 2)))
        }
    }
    func next(receiver: String, passStarted: Double, limit: Int) throws -> [QueueAsset] {
        guard limit > 0 else { return [] }
        // Reserve one slot for a due failure and one for an integrity audit. Fixed
        // pass cutoffs prevent repeated failures/audits looping in one long pass.
        let retry = try select(Self.retrySQL, [receiver, receiver, String(passStarted)])
        let audit = limit > retry.count ? try select(Self.auditSQL, [receiver, receiver, String(passStarted - 86400)]) : []
        let count = max(0, limit - retry.count - audit.count)
        let pending = try select(Self.pendingSQL, [receiver, receiver, String(count)])
        return pending + retry + audit
    }
    func complete(_ asset: QueueAsset, receiver: String, now: Double) throws {
        try execute(Self.completeSQL, [String(now), receiver, asset.identifier, asset.version])
    }
    func fail(_ asset: QueueAsset, receiver: String, now: Double) throws {
        try execute(Self.failureSQL, [String(now), receiver, asset.identifier, asset.version])
    }
    func remaining(receiver: String) throws -> Int {
        let query = try statement(Self.remainingSQL, [receiver, receiver])
        defer { sqlite3_finalize(query) }
        guard sqlite3_step(query) == SQLITE_ROW else { throw NSError(domain: "AssetSyncQueue", code: 6) }
        return Int(sqlite3_column_int64(query, 0))
    }
    func snapshotGeneration(receiver: String) throws -> String? {
        let query = try statement(Self.snapshotSQL, [receiver])
        defer { sqlite3_finalize(query) }
        let result = sqlite3_step(query)
        if result == SQLITE_DONE { return nil }
        guard result == SQLITE_ROW, let value = sqlite3_column_text(query, 0) else {
            throw NSError(domain: "AssetSyncQueue", code: 6)
        }
        return String(cString: value)
    }
}
