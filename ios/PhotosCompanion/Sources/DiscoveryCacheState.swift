import Foundation

/// Process-local reuse only. A new process always needs a full discovery.
/// PhotoKit owns the change notifications; this value is used under the
/// tracker's lock so a change during discovery cannot be acknowledged by it.
struct DiscoveryCacheState {
    struct Context: Equatable, Sendable {
        let receiver: String
        let authorization: Int
    }
    struct Ticket: Sendable {
        let context: Context
        let revision: UInt64
    }
    let maximumAge: TimeInterval
    private var context: Context?
    private var revision: UInt64 = 0
    private var completedRevision: UInt64?
    private var completedAt: TimeInterval?

    init(maximumAge: TimeInterval = 6 * 60 * 60) { self.maximumAge = maximumAge }

    mutating func invalidate() { revision &+= 1 }

    func isCurrent(context requested: Context, now: TimeInterval) -> Bool {
        guard context == requested, completedRevision == revision, let completedAt else { return false }
        return now >= completedAt && now - completedAt < maximumAge
    }
    mutating func begin(context requested: Context, now: TimeInterval) -> Ticket? {
        if context != requested {
            context = requested
            completedAt = nil
            completedRevision = nil
            invalidate()
        }
        guard !isCurrent(context: requested, now: now) else { return nil }
        // Reserve a distinct revision even for an age-based refresh. Late
        // completion/failure of an older reservation must not acknowledge it.
        invalidate()
        return Ticket(context: requested, revision: revision)
    }
    @discardableResult
    mutating func finish(_ ticket: Ticket, now: TimeInterval) -> Bool {
        guard context == ticket.context, revision == ticket.revision else { return false }
        completedRevision = ticket.revision
        completedAt = now
        return true
    }
    mutating func fail(_ ticket: Ticket) {
        if context == ticket.context && revision == ticket.revision { invalidate() }
    }
}
