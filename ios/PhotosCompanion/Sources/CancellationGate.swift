import Foundation

/// Cancellation may arrive before a callback-based operation has installed its action.
final class CancellationGate: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    private var action: (() -> Void)?

    func install(_ action: @escaping () -> Void) {
        lock.lock()
        let run = cancelled
        if !run { self.action = action }
        lock.unlock()
        if run { action() }
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let run = action
        action = nil
        lock.unlock()
        run?()
    }
}
