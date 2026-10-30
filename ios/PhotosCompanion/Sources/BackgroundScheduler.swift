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
            let job = Task { @MainActor in
                let success = await SyncState.shared.syncNow(maxAssets: 20)
                task.setTaskCompleted(success: success)
                if SyncState.shared.automatic { schedule() }
            }
            task.expirationHandler = { job.cancel() }
        }
    }

    static func schedule() {
        let request = BGProcessingTaskRequest(identifier: identifier)
        request.requiresNetworkConnectivity = true
        request.requiresExternalPower = false
        request.earliestBeginDate = Date(timeIntervalSinceNow: 60 * 30)
        try? BGTaskScheduler.shared.submit(request)
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
