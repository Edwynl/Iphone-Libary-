import Foundation
import UIKit

/// File-backed URLSession tasks survive ordinary app suspension. Each task uploads
/// ONE bounded chunk. After a crash, the next pass asks Windows for its offset.
/// Apple does not restart transfers after the user force-quits the app.
final class BackgroundChunkUploader: NSObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    static let shared = BackgroundChunkUploader()

    struct Result {
        let status: Int
        let body: Data
    }
    private struct Pending {
        var responseBody: Data
        let file: URL
        let continuation: CheckedContinuation<Result, Error>
    }
    private let lock = NSLock()
    private var pending: [Int: Pending] = [:]
    private var backgroundEventsFinished: (() -> Void)?

    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.background(
            withIdentifier: "com.edwyn.windowsaiphotos.companion.uploads")
        configuration.sessionSendsLaunchEvents = true
        configuration.isDiscretionary = false
        configuration.waitsForConnectivity = true
        configuration.httpMaximumConnectionsPerHost = 2
        return URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }()

    func installBackgroundCompletionHandler(_ completion: @escaping () -> Void) {
        lock.lock()
        backgroundEventsFinished = completion
        lock.unlock()
        _ = session // Reconnect to daemon tasks on relaunch.
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        lock.lock()
        let done = backgroundEventsFinished
        backgroundEventsFinished = nil
        lock.unlock()
        DispatchQueue.main.async { done?() }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.lock()
        if var entry = pending[dataTask.taskIdentifier] {
            // Sync endpoint responses are small. Do not accumulate arbitrary server data.
            if entry.responseBody.count + data.count < 65536 {
                entry.responseBody.append(data)
                pending[dataTask.taskIdentifier] = entry
            }
        }
        lock.unlock()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock()
        let entry = pending.removeValue(forKey: task.taskIdentifier)
        lock.unlock()
        // After a process relaunch, an old upload may have no in-memory continuation.
        // It still completes on Windows; resume later by querying its committed offset.
        guard let entry else { return }
        try? FileManager.default.removeItem(at: entry.file)
        if let error { entry.continuation.resume(throwing: error); return }
        let response = task.response as? HTTPURLResponse
        entry.continuation.resume(returning: Result(status: response?.statusCode ?? 0,
                                                     body: entry.responseBody))
    }

    func outstandingTaskCount() async -> Int {
        await withCheckedContinuation { continuation in
            session.getAllTasks { continuation.resume(returning: $0.count) }
        }
    }

    func waitForPreviousTasks() async throws {
        // A newly relaunched process should not race with a transfer still running
        // in the iOS networking daemon. Never delete or overwrite the source photo.
        for _ in 0..<30 {
            if await outstandingTaskCount() == 0 { return }
            try Task.checkCancellation()
            try await Task.sleep(nanoseconds: 1_000_000_000)
        }
        throw NSError(domain: "PhotoSync", code: 9,
                      userInfo: [NSLocalizedDescriptionKey: "Previous background upload still running; retry later"])
    }

    func upload(request: URLRequest, bytes: Data) async throws -> Result {
        let folder = FileManager.default.urls(for: .applicationSupportDirectory,
                                              in: .userDomainMask)[0]
            .appendingPathComponent("upload-chunks", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let file = folder.appendingPathComponent(UUID().uuidString + ".part")
        try bytes.write(to: file, options: .atomic)
        return try await withCheckedThrowingContinuation { continuation in
            let task = session.uploadTask(with: request, fromFile: file)
            lock.lock()
            pending[task.taskIdentifier] = Pending(responseBody: Data(), file: file,
                                                   continuation: continuation)
            lock.unlock()
            task.resume()
        }
    }
}
