import Foundation
import XCTest
@testable import PhotosSyncQueue

final class AssetSyncQueueTests: XCTestCase {
    private func withQueue(_ run: (AssetSyncQueue) async throws -> Void) async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let queue = try AssetSyncQueue(url: folder.appendingPathComponent("queue.sqlite"))
        try await run(queue)
    }
    func testNewOldDatedImportPrecedesBacklog() async throws {
        try await withQueue { queue in
            let old = (0..<100).map { QueueAsset(identifier: "old\($0)", version: "v1", changedAt: Double(100 - $0)) }
            try await queue.discover(old, receiver: "r", generation: "g", now: 1000)
            try await queue.finishDiscovery(receiver: "r", generation: "g")
            try await queue.complete(old[0], receiver: "r", now: 1100)
            let fresh = QueueAsset(identifier: "fresh-old-exif", version: "v1", changedAt: 0)
            try await queue.discover(Array(old.reversed()) + [fresh], receiver: "r", generation: "g2", now: 1200)
            try await queue.finishDiscovery(receiver: "r", generation: "g2")
            let jobs = try await queue.next(receiver: "r", passStarted: 1201, limit: 20)
            XCTAssertEqual(jobs.first, fresh)
            XCTAssertFalse(jobs.contains(old[0]))
        }
    }
    func testReservationsRetryBackoffAndReceiverIsolation() async throws {
        try await withQueue { queue in
            let assets = (0..<30).map { QueueAsset(identifier: "a\($0)", version: "v1", changedAt: Double($0)) }
            for receiver in ["r", "other"] {
                try await queue.discover(assets, receiver: receiver, generation: "g", now: 1000)
                try await queue.finishDiscovery(receiver: receiver, generation: "g")
            }
            try await queue.fail(assets[0], receiver: "r", now: 1000)
            try await queue.complete(assets[1], receiver: "r", now: 1000)
            let jobs = try await queue.next(receiver: "r", passStarted: 100000, limit: 20)
            XCTAssertEqual(jobs.count, 20)
            XCTAssertEqual(Array(jobs.suffix(2)), [assets[0], assets[1]])
            try await queue.fail(assets[0], receiver: "r", now: 100001)
            try await queue.complete(assets[1], receiver: "r", now: 100001)
            let samePass = try await queue.next(receiver: "r", passStarted: 100000, limit: 20)
            XCTAssertFalse(samePass.contains(assets[0]))
            XCTAssertFalse(samePass.contains(assets[1]))
            let otherRemaining = try await queue.remaining(receiver: "other")
            XCTAssertEqual(otherRemaining, 30)
        }
    }
    func testQueueSurvivesReopenAndIgnoresRemovedIDs() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let url = folder.appendingPathComponent("queue.sqlite")
        let asset = QueueAsset(identifier: "stable", version: "v1", changedAt: 1)
        var queue: AssetSyncQueue? = try AssetSyncQueue(url: url)
        try await queue!.discover([asset], receiver: "r", generation: "g", now: 1)
        try await queue!.finishDiscovery(receiver: "r", generation: "g")
        queue = nil
        let reopened = try AssetSyncQueue(url: url)
        let jobs = try await reopened.next(receiver: "r", passStarted: 2, limit: 20)
        XCTAssertEqual(jobs, [asset])
        try await reopened.finishDiscovery(receiver: "r", generation: "empty")
        let remaining = try await reopened.remaining(receiver: "r")
        XCTAssertEqual(remaining, 0)
    }

    func testTwentySuccessfulAssetsDoNotCompleteAHundredAssetSnapshot() async throws {
        try await withQueue { queue in
            let assets = (0..<100).map { QueueAsset(identifier: "a\($0)", version: "v1", changedAt: Double($0)) }
            try await queue.discover(assets, receiver: "r", generation: "current", now: 1)
            try await queue.finishDiscovery(receiver: "r", generation: "current")
            for asset in assets.prefix(20) { try await queue.complete(asset, receiver: "r", now: 2) }
            let partial = try await queue.remaining(receiver: "r")
            XCTAssertEqual(partial, 80)
            let generation = try await queue.snapshotGeneration(receiver: "r")
            XCTAssertEqual(generation, "current")
            for asset in assets.dropFirst(20) { try await queue.complete(asset, receiver: "r", now: 3) }
            let complete = try await queue.remaining(receiver: "r")
            XCTAssertEqual(complete, 0)
        }
    }
}
