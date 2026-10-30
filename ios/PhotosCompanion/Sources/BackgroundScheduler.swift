import Foundation
import UIKit
import BackgroundTasks

/// iOS decides when a BGProcessingTask executes; no fixed synchronization interval
/// or guaranteed wakeup is possible. File uploads already queued may continue.
enum BackgroundScheduler {
    static let identifier = "com.edwyn.windowsaiphotos.companion.sync"

    static func register() {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: identifier, using: nil) { task in
            guard let task = task as? BGProcessingTask else {
                task.setTaskCompleted(success: false)
                return
            }
            let completion = BackgroundCompletion(task)
            let job = Task { @MainActor in
                if Task.isCancelled { completion.finish(false); return }
                guard SyncState.shared.automatic && SyncState.shared.isPaired else {
                    completion.finish(false)
                    return
                }
                SyncState.shared.scheduleBackground() // Surface rejected scheduling to the user.
                let success = await SyncState.shared.automaticPass(maxAssets: 20)
                completion.finish(success)
            }
            task.expirationHandler = { job.cancel(); completion.finish(false) }
        }
    }

    static func cancel() {
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: identifier)
    }

    static func schedule() throws {
        let request = BGProcessingTaskRequest(identifier: identifier)
        request.requiresNetworkConnectivity = true
        request.requiresExternalPower = false
        request.earliestBeginDate = Date(timeIntervalSinceNow: 60 * 30)
        try BGTaskScheduler.shared.submit(request)
    }
}

private final class BackgroundCompletion: @unchecked Sendable {
    private let lock = NSLock()
    private var completed = false
    private let task: BGTask
    init(_ task: BGTask) { self.task = task }
    func finish(_ success: Bool) {
        lock.lock()
        guard !completed else { lock.unlock(); return }
        completed = true
        lock.unlock()
        task.setTaskCompleted(success: success)
    }
}

final class PhotosAppDelegate: UIResponder, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        BackgroundScheduler.register()
        return true
    }

    func application(_ application: UIApplication,
                     handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        guard identifier == "com.edwyn.windowsaiphotos.companion.uploads" else {
            completionHandler()
            return
        }
        BackgroundChunkUploader.shared.installBackgroundCompletionHandler(completionHandler)
    }
}
