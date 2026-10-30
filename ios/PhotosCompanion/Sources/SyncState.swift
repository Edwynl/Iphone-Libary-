import Foundation
import SwiftUI
import Photos
import Network
import UIKit

@MainActor
final class SyncState: ObservableObject {
    static let shared = SyncState()
    private let settings = UserDefaults.standard
    private let cursorKey = "photoScanCursor"
    private let deviceIdKey = "deviceIdentifier"

    @Published var serverURL: String {
        didSet { settings.set(serverURL, forKey: "serverURL") }
    }
    @Published var automatic: Bool {
        didSet {
            settings.set(automatic, forKey: "automatic")
            if automatic { BackgroundScheduler.schedule() }
        }
    }
    @Published var wifiOnly: Bool {
        didSet { settings.set(wifiOnly, forKey: "wifiOnly") }
    }
    @Published private(set) var isPaired: Bool = false
    @Published private(set) var isRunning = false
    @Published private(set) var processedAssets = 0
    @Published private(set) var uploadedResources = 0
    @Published private(set) var totalAssets = 0
    @Published private(set) var progress = "Not synced yet"
    @Published private(set) var lastError = ""
    @Published private(set) var lastCompleted: Date?

    let deviceId: String
    private var checkpoint: CheckpointStore?

    private init() {
        serverURL = settings.string(forKey: "serverURL") ?? ""
        automatic = settings.bool(forKey: "automatic")
        wifiOnly = settings.object(forKey: "wifiOnly") as? Bool ?? true
        let persistedId = settings.string(forKey: deviceIdKey)
        deviceId = persistedId ?? UUID().uuidString
        if persistedId == nil { settings.set(deviceId, forKey: deviceIdKey) }
        isPaired = KeychainStore.loadToken() != nil
        lastCompleted = settings.object(forKey: "lastCompleted") as? Date
        do { checkpoint = try CheckpointStore() }
        catch { lastError = "Cannot open local sync checkpoints: \(error.localizedDescription)" }
    }

    func pair(masterToken: String) async {
        guard !masterToken.isEmpty else { lastError = "Enter Windows pairing token"; return }
        do {
            let client = try SyncAPI(serverURL: serverURL, wifiOnly: wifiOnly)
            let token = try await client.pair(masterToken: masterToken, deviceId: deviceId,
                                               deviceName: UIDevice.current.name)
            try KeychainStore.saveToken(token)
            isPaired = true
            lastError = ""
            progress = "Paired successfully. You can start original-quality sync."
            if automatic { BackgroundScheduler.schedule() }
        } catch { lastError = error.localizedDescription }
    }

    func disconnect() {
        automatic = false
        KeychainStore.removeToken()
        isPaired = false
        progress = "Disconnected locally. Revoke device in Windows to invalidate old tokens."
    }

    func syncNow(maxAssets: Int = Int.max) async -> Bool {
        if isRunning { return false }
        isRunning = true
        lastError = ""
        processedAssets = 0
        uploadedResources = 0
        defer { isRunning = false }
        do {
            guard let token = KeychainStore.loadToken() else { throw SyncAPIError.notPaired }
            guard let checkpoint else {
                throw NSError(domain: "PhotosCompanion", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "Local checkpoint database is unavailable"])
            }
            let client = try SyncAPI(serverURL: serverURL, wifiOnly: wifiOnly)
            if wifiOnly && !(await WiFiAvailability.isConnected()) {
                throw NSError(domain: "PhotosCompanion", code: 2,
                              userInfo: [NSLocalizedDescriptionKey: "Connect to Wi-Fi or disable Wi-Fi-only sync"])
            }
            let permission = await PhotoSource.requestReadAccess()
            guard permission == .authorized || permission == .limited else {
                throw NSError(domain: "PhotosCompanion", code: 3,
                              userInfo: [NSLocalizedDescriptionKey: "Allow Photos access in iOS Settings"])
            }
            if permission == .limited {
                progress = "Limited Photos access: only selected assets can be backed up."
            }
            let assets = PhotoSource.allAssets()
            totalAssets = assets.count
            if totalAssets == 0 { progress = "No photos or videos available"; return true }
            var index = settings.integer(forKey: cursorKey)
            if index >= totalAssets || index < 0 { index = 0 }
            let stop = maxAssets >= totalAssets - index ? totalAssets : index + maxAssets
            while index < stop {
                try Task.checkCancellation()
                let asset = assets.object(at: index)
                let resources = PhotoSource.resourceList(for: asset)
                for resource in resources {
                    try Task.checkCancellation()
                    if checkpoint.contains(resource.assetKey) { continue }
                    progress = "Exporting \(resource.filename) (\(index + 1)/\(totalAssets))"
                    let exported = try await PhotoSource.export(resource)
                    defer { try? FileManager.default.removeItem(at: exported.fileURL) }
                    progress = "Uploading \(resource.filename) (\(index + 1)/\(totalAssets))"
                    try await client.upload(resource, exported: exported, deviceToken: token)
                    try checkpoint.markCompleted(resource.assetKey)
                    uploadedResources += 1
                }
                index += 1
                processedAssets += 1
                settings.set(index, forKey: cursorKey)
                progress = "Checked \(index)/\(totalAssets) assets; sent \(uploadedResources) original resources"
            }
            if index == totalAssets { settings.set(0, forKey: cursorKey) }
            lastCompleted = Date()
            settings.set(lastCompleted, forKey: "lastCompleted")
            progress = "Sync pass completed; \(uploadedResources) new resources backed up."
            return true
        } catch {
            lastError = error.localizedDescription
            progress = "Stopped safely. Reconnect and retry; transfer resumes from Windows offset."
            return false
        }
    }
}

enum WiFiAvailability {
    static func isConnected() async -> Bool {
        await withCheckedContinuation { continuation in
            let monitor = NWPathMonitor(requiredInterfaceType: .wifi)
            let lock = NSLock()
            var finished = false
            monitor.pathUpdateHandler = { path in
                lock.lock()
                if finished { lock.unlock(); return }
                finished = true
                lock.unlock()
                monitor.cancel()
                continuation.resume(returning: path.status == .satisfied)
            }
            monitor.start(queue: DispatchQueue(label: "photos-companion.wifi-check"))
        }
    }
}
