import Foundation
import SwiftUI
import Photos
import Network
import UIKit

@MainActor
final class SyncState: ObservableObject {
    static let shared = SyncState()
    private let settings = UserDefaults.standard
    private let deviceIdKey = "deviceIdentifier"

    @Published var serverURL: String {
        didSet {
            settings.set(serverURL, forKey: "serverURL")
            let origin = try? SyncAPI(serverURL: serverURL, wifiOnly: wifiOnly).origin
            if isPaired && origin != settings.string(forKey: "pairedServerURL") { disconnect() }
        }
    }
    @Published var automatic: Bool {
        didSet {
            settings.set(automatic, forKey: "automatic")
            if automatic { scheduleBackground() }
            else { BackgroundScheduler.cancel(); automaticTask?.cancel(); schedulingError = "" }
        }
    }
    @Published var wifiOnly: Bool {
        didSet {
            settings.set(wifiOnly, forKey: "wifiOnly")
            if wifiOnly != oldValue { operationTask?.cancel() }
        }
    }
    @Published private(set) var isPaired: Bool = false
    @Published private(set) var isRunning = false
    @Published private(set) var processedAssets = 0
    @Published private(set) var uploadedResources = 0
    @Published private(set) var totalAssets = 0
    @Published private(set) var progress = "Not synced yet"
    @Published private(set) var lastError = ""
    @Published private(set) var schedulingError = ""
    @Published private(set) var lastCompleted: Date?
    @Published private(set) var lastSuccessfulBatch: Date?
    @Published private(set) var remainingAssets: Int?
    @Published private(set) var snapshotNeedsRefresh = true

    let deviceId: String
    private var checkpoint: CheckpointStore?
    private var assetQueue: AssetSyncQueue?
    private var libraryTracker: PhotoLibraryTracker?
    private var operationTask: Task<Bool, Never>?
    private var automaticTask: Task<Bool, Never>?

    private init() {
        serverURL = settings.string(forKey: "serverURL") ?? ""
        automatic = settings.bool(forKey: "automatic")
        wifiOnly = settings.object(forKey: "wifiOnly") as? Bool ?? true
        let persistedId = settings.string(forKey: deviceIdKey)
        deviceId = persistedId ?? UUID().uuidString
        if persistedId == nil { settings.set(deviceId, forKey: deviceIdKey) }
        isPaired = KeychainStore.loadToken() != nil && settings.string(forKey: "pairedServerURL") != nil && settings.string(forKey: "receiverID") != nil
        do { checkpoint = try CheckpointStore(); assetQueue = try AssetSyncQueue() }
        catch { lastError = "Cannot open local sync checkpoints: \(error.localizedDescription)" }
        loadSyncHistory(receiver: isPaired ? settings.string(forKey: "receiverID") : nil)
    }

    func pair(masterToken: String) async {
        guard !isRunning else { lastError = "Wait for the current sync to stop before pairing"; return }
        guard !masterToken.isEmpty else { lastError = "Enter Windows pairing token"; return }
        do {
            let client = try SyncAPI(serverURL: serverURL, wifiOnly: wifiOnly)
            let reply = try await client.pair(masterToken: masterToken, deviceId: deviceId,
                                               deviceName: UIDevice.current.name)
            try KeychainStore.saveToken(reply.device_token)
            settings.set(client.origin, forKey: "pairedServerURL")
            settings.set(reply.receiver_id, forKey: "receiverID")
            libraryTracker?.invalidate()
            loadSyncHistory(receiver: reply.receiver_id)
            isPaired = true
            lastError = ""
            progress = "Paired successfully. You can start original-quality sync."
            if automatic { scheduleBackground() }
        } catch { lastError = error.localizedDescription }
    }

    private func historyKey(_ field: String, receiver: String) -> String {
        "syncHistoryV2.\(receiver).\(field)"
    }

    private func loadSyncHistory(receiver: String?) {
        lastCompleted = receiver.flatMap { settings.object(forKey: historyKey("snapshotCompleted", receiver: $0)) as? Date }
        lastSuccessfulBatch = receiver.flatMap { settings.object(forKey: historyKey("batchCompleted", receiver: $0)) as? Date }
        totalAssets = 0
        markProgressUnknown()
    }

    private func markProgressUnknown() {
        remainingAssets = nil
        snapshotNeedsRefresh = true
    }

    func librarySelectionMayHaveChanged() {
        libraryTracker?.invalidate()
        markProgressUnknown()
    }

    func scheduleBackground() {
        do { try BackgroundScheduler.schedule(); schedulingError = "" }
        catch { schedulingError = "Background scheduling failed: \(error.localizedDescription). Open the app to sync and retry the automatic-sync toggle." }
    }

    func disconnect() {
        automatic = false
        operationTask?.cancel()
        settings.removeObject(forKey: "pairedServerURL")
        settings.removeObject(forKey: "receiverID")
        KeychainStore.removeToken()
        libraryTracker?.stopObserving()
        libraryTracker = nil
        loadSyncHistory(receiver: nil)
        isPaired = false
        progress = "Disconnected locally. Revoke device in Windows to invalidate old tokens."
    }

    func automaticPass(maxAssets: Int) async -> Bool {
        guard automatic && isPaired && !isRunning else { return false }
        let task = Task { await self.syncNow(maxAssets: maxAssets) }
        automaticTask = task
        defer { automaticTask = nil }
        return await withTaskCancellationHandler { await task.value } onCancel: { task.cancel() }
    }

    func syncNow(maxAssets: Int = Int.max) async -> Bool {
        guard !isRunning && maxAssets > 0 else { return false }
        isRunning = true
        markProgressUnknown()
        let task = Task { await self.performSync(maxAssets: maxAssets) }
        operationTask = task
        defer { isRunning = false; operationTask = nil }
        return await withTaskCancellationHandler { await task.value } onCancel: { task.cancel() }
    }

    private func performSync(maxAssets: Int) async -> Bool {
        lastError = ""
        processedAssets = 0
        uploadedResources = 0
        do {
            try Task.checkCancellation()
            guard isPaired, let token = KeychainStore.loadToken(),
                  let receiver = settings.string(forKey: "receiverID") else { throw SyncAPIError.notPaired }
            guard let checkpoint, let assetQueue else { throw CocoaError(.fileReadUnknown) }
            let client = try SyncAPI(serverURL: serverURL, wifiOnly: wifiOnly)
            guard client.origin == settings.string(forKey: "pairedServerURL") else { throw SyncAPIError.notPaired }
            if wifiOnly {
                let available = await WiFiAvailability.isConnected()
                if !available {
                    throw NSError(domain: "PhotosCompanion", code: 2,
                                  userInfo: [NSLocalizedDescriptionKey: "Connect to Wi-Fi or disable Wi-Fi-only sync"])
                }
            }
            let permission = await PhotoSource.requestReadAccess()
            guard permission == .authorized || permission == .limited else {
                throw NSError(domain: "PhotosCompanion", code: 3,
                              userInfo: [NSLocalizedDescriptionKey: "Allow Photos access in iOS Settings"])
            }
            try await BackgroundChunkUploader.shared.waitForPreviousTasks()
            try PhotoSource.cleanAbandonedExports()
            if libraryTracker == nil {
                libraryTracker = PhotoLibraryTracker { [weak self] in
                    Task { @MainActor [weak self] in self?.markProgressUnknown() }
                }
            }
            guard let tracker = libraryTracker else { throw CocoaError(.fileReadUnknown) }
            // A user-requested full sync refreshes metadata even without a notification.
            if maxAssets == Int.max { tracker.invalidate() }
            progress = "Checking your Photos selection…"
            let passStarted = Date().timeIntervalSince1970
            var jobs: [QueueAsset] = []
            var passHadFailure = false
            while processedAssets < maxAssets {
                try Task.checkCancellation()
                let access = PHPhotoLibrary.authorizationStatus(for: .readWrite)
                guard access == .authorized || access == .limited else { throw CocoaError(.fileReadNoPermission) }
                if let count = try await tracker.reconcile(queue: assetQueue, receiver: receiver, authorization: access.rawValue) {
                    totalAssets = count
                    jobs.removeAll() // Re-select new/changed assets ahead of the backlog.
                }
                try Task.checkCancellation()
                if jobs.isEmpty {
                    jobs = try await assetQueue.next(receiver: receiver, passStarted: passStarted,
                                                     limit: min(20, maxAssets - processedAssets))
                }
                guard !jobs.isEmpty else { break }
                let job = jobs.removeFirst()
                let fetched = PHAsset.fetchAssets(withLocalIdentifiers: [job.identifier], options: nil)
                guard let asset = fetched.firstObject else {
                    // It may have been removed from the selected library. Never delete
                    // an original or mark the asset backed up merely because it vanished.
                    try await assetQueue.fail(job, receiver: receiver, now: Date().timeIntervalSince1970)
                    passHadFailure = true
                    processedAssets += 1
                    continue
                }
                guard PhotoSource.queueAsset(asset).version == job.version else {
                    // A change notification may still be in flight. Retry next pass.
                    try await assetQueue.fail(job, receiver: receiver, now: Date().timeIntervalSince1970)
                    passHadFailure = true
                    processedAssets += 1
                    continue
                }
                let resources = PhotoSource.resourceList(for: asset)
                var assetSucceeded = !resources.isEmpty
                let unhandledKey = asset.localIdentifier + "::unhandled"
                if resources.isEmpty {
                    try checkpoint.recordFailure(unhandledKey, receiver: receiver, message: "No supported original resources found")
                } else { try checkpoint.clearFailure(unhandledKey, receiver: receiver) }
                for resource in resources {
                    try Task.checkCancellation()
                    let access = PHPhotoLibrary.authorizationStatus(for: .readWrite)
                    guard access == .authorized || access == .limited else { throw CocoaError(.fileReadNoPermission) }
                    guard isPaired, receiver == settings.string(forKey: "receiverID"),
                          client.origin == settings.string(forKey: "pairedServerURL") else { throw SyncAPIError.notPaired }
                    do {
                        if let saved = try checkpoint.completed(resource.failureKey, receiver: receiver, version: resource.assetKey),
                           try await client.verify(resource, checkpoint: saved, receiver: receiver, token: token) {
                            try checkpoint.clearFailure(resource.failureKey, receiver: receiver)
                            continue
                        }
                        if wifiOnly {
                            let available = await WiFiAvailability.isConnected()
                            if !available { throw URLError(.notConnectedToInternet) }
                        }
                        progress = "Exporting \(resource.filename) (checked \(processedAssets)/\(totalAssets))"
                        let exported = try await PhotoSource.export(resource, wifiOnly: wifiOnly)
                        defer { try? FileManager.default.removeItem(at: exported.fileURL) }
                        try Task.checkCancellation()
                        progress = "Uploading \(resource.filename) (checked \(processedAssets)/\(totalAssets))"
                        try await client.upload(resource, exported: exported, deviceToken: token)
                        try checkpoint.markCompleted(resource.failureKey, receiver: receiver, version: resource.assetKey, exported: exported)
                        uploadedResources += 1
                    } catch {
                        if Task.isCancelled || error is CancellationError { throw CancellationError() }
                        if error is URLError { throw error }
                        if let apiError = error as? SyncAPIError {
                            switch apiError {
                            case .notPaired: throw error
                            case .http(let status, _) where [401, 403, 423, 429, 503, 507].contains(status): throw error
                            default: break
                            }
                        }
                        try checkpoint.recordFailure(resource.failureKey, receiver: receiver, message: error.localizedDescription)
                        lastError = "Unresolved resource: \(resource.filename): \(error.localizedDescription)"
                        assetSucceeded = false
                    }
                }
                processedAssets += 1
                if assetSucceeded {
                    try await assetQueue.complete(job, receiver: receiver, now: Date().timeIntervalSince1970)
                } else {
                    try await assetQueue.fail(job, receiver: receiver, now: Date().timeIntervalSince1970)
                    passHadFailure = true
                }
            }
            try Task.checkCancellation()
            let remaining = try await assetQueue.remaining(receiver: receiver)
            let generation = try await assetQueue.snapshotGeneration(receiver: receiver)
            try Task.checkCancellation()
            guard isPaired, receiver == settings.string(forKey: "receiverID"),
                  client.origin == settings.string(forKey: "pairedServerURL") else { throw SyncAPIError.notPaired }
            let current = tracker.isCurrent(receiver: receiver,
                authorization: PHPhotoLibrary.authorizationStatus(for: .readWrite).rawValue)
            remainingAssets = current ? remaining : nil
            snapshotNeedsRefresh = !current
            if !passHadFailure && current {
                if processedAssets > 0 {
                    let now = Date()
                    lastSuccessfulBatch = now
                    settings.set(now, forKey: historyKey("batchCompleted", receiver: receiver))
                }
                if remaining == 0, let generation,
                   settings.string(forKey: historyKey("completedGeneration", receiver: receiver)) != generation {
                    let now = Date()
                    lastCompleted = now
                    settings.set(now, forKey: historyKey("snapshotCompleted", receiver: receiver))
                    settings.set(generation, forKey: historyKey("completedGeneration", receiver: receiver))
                }
            }
            let backlog = current ? "\(remaining) pending or failed." : "Photos changed; backlog needs refreshing."
            progress = "Checked \(processedAssets) assets; sent \(uploadedResources) originals; " + backlog
            // Reaching this opportunity's budget is normal progress, not a failed
            // BGProcessingTask. Only the full snapshot updates lastCompleted.
            return !passHadFailure && current
        } catch {
            markProgressUnknown()
            lastError = error.localizedDescription
            progress = Task.isCancelled ? "Sync cancelled safely; committed chunks remain resumable." : "Pass interrupted; reconnect and retry."
            return false
        }
    }
}

enum WiFiAvailability {
    static func isConnected() async -> Bool {
        let gate = CancellationGate()
        return await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                let monitor = NWPathMonitor(requiredInterfaceType: .wifi)
                let lock = NSLock()
                var finished = false
                let finish: (Bool) -> Void = { connected in
                    lock.lock()
                    if finished { lock.unlock(); return }
                    finished = true
                    lock.unlock()
                    monitor.cancel()
                    continuation.resume(returning: connected)
                }
                monitor.pathUpdateHandler = { finish($0.status == .satisfied) }
                let queue = DispatchQueue(label: "photos-companion.wifi-check")
                monitor.start(queue: queue)
                gate.install { finish(false) }
                queue.asyncAfter(deadline: .now() + 5) { finish(false) }
            }
        } onCancel: { gate.cancel() }
    }
}
