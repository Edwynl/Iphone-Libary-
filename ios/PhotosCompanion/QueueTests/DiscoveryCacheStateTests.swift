import Foundation
import XCTest
@testable import PhotosSyncQueue

final class DiscoveryCacheStateTests: XCTestCase {
    private let receiver = DiscoveryCacheState.Context(receiver: "r", authorization: 3)

    func testUnchangedSmallBatchesReuseDiscoveryWithinTheProcess() throws {
        var cache = DiscoveryCacheState()
        let ticket = try XCTUnwrap(cache.begin(context: receiver, now: 100))
        XCTAssertFalse(cache.isCurrent(context: receiver, now: 100))
        XCTAssertTrue(cache.finish(ticket, now: 101))
        // 100k assets at a 20-asset processing budget need 5k batches. An
        // unchanged observation must not require 5k full metadata discoveries.
        for batch in 0..<5000 {
            XCTAssertNil(cache.begin(context: receiver, now: 102 + Double(batch)))
        }
    }

    func testChangeArrivingDuringDiscoveryCannotBeClearedByCompletion() throws {
        var cache = DiscoveryCacheState()
        let old = try XCTUnwrap(cache.begin(context: receiver, now: 1))
        cache.invalidate()
        XCTAssertFalse(cache.finish(old, now: 2))
        let fresh = try XCTUnwrap(cache.begin(context: receiver, now: 3))
        XCTAssertTrue(cache.finish(fresh, now: 4))
        XCTAssertTrue(cache.isCurrent(context: receiver, now: 5))
    }

    func testFailedOrCancelledDiscoveryNeedsRetryAndLateFailureIsIgnored() throws {
        var cache = DiscoveryCacheState()
        let cancelled = try XCTUnwrap(cache.begin(context: receiver, now: 1))
        cache.fail(cancelled)
        let retry = try XCTUnwrap(cache.begin(context: receiver, now: 2))
        XCTAssertFalse(cache.finish(cancelled, now: 3))
        XCTAssertTrue(cache.finish(retry, now: 4))
        cache.fail(cancelled)
        XCTAssertTrue(cache.isCurrent(context: receiver, now: 5))
    }

    func testReceiverAndAuthorizationChangesNeverReuseAnotherContext() throws {
        var cache = DiscoveryCacheState()
        let initial = try XCTUnwrap(cache.begin(context: receiver, now: 1))
        XCTAssertTrue(cache.finish(initial, now: 2))
        let other = DiscoveryCacheState.Context(receiver: "other", authorization: 3)
        let changed = try XCTUnwrap(cache.begin(context: other, now: 3))
        XCTAssertFalse(cache.isCurrent(context: receiver, now: 3))
        XCTAssertFalse(cache.finish(initial, now: 4))
        XCTAssertTrue(cache.finish(changed, now: 5))
        let limited = DiscoveryCacheState.Context(receiver: "other", authorization: 4)
        XCTAssertNotNil(cache.begin(context: limited, now: 6))
    }

    func testColdStartManualInvalidationAgeLimitAndClockRollbackRequireDiscovery() throws {
        var cache = DiscoveryCacheState(maximumAge: 60)
        let first = try XCTUnwrap(cache.begin(context: receiver, now: 100))
        XCTAssertTrue(cache.finish(first, now: 101))
        XCTAssertNil(cache.begin(context: receiver, now: 160))
        let expired = try XCTUnwrap(cache.begin(context: receiver, now: 161))
        XCTAssertFalse(cache.finish(first, now: 162))
        XCTAssertTrue(cache.finish(expired, now: 163))
        XCTAssertNotNil(cache.begin(context: receiver, now: 99))
        cache.invalidate()
        XCTAssertNotNil(cache.begin(context: receiver, now: 164))
        var newProcess = DiscoveryCacheState()
        XCTAssertNotNil(newProcess.begin(context: receiver, now: 165))
    }
}
