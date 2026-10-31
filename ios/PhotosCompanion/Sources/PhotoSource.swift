import Foundation
import Photos
import CryptoKit

struct PhotoResource {
    let assetKey: String
    let assetIdentifier: String
    let kind: String
    let filename: String
    let createdAt: String?
    let modifiedAt: String?
    let resource: PHAssetResource
    var failureKey: String { [assetIdentifier, kind, filename].joined(separator: "::") }
}

struct ExportedPhotoResource {
    let fileURL: URL
    let size: Int64
    let sha256: String
}

/// PhotoKit supplies changes on its own queue. Keep the fetch result behind a
/// lock; only primitive metadata crosses into the persistent queue actor.
final class PhotoLibraryTracker: NSObject, PHPhotoLibraryChangeObserver, @unchecked Sendable {
    private let lock = NSLock()
    private var result: PHFetchResult<PHAsset>?
    private var cache = DiscoveryCacheState()
    private var observing = true
    private let onChange: @Sendable () -> Void

    init(onChange: @escaping @Sendable () -> Void = {}) {
        self.onChange = onChange
        super.init()
        PHPhotoLibrary.shared().register(self)
    }
    deinit { if observing { PHPhotoLibrary.shared().unregisterChangeObserver(self) } }
    func invalidate() {
        lock.lock()
        cache.invalidate()
        lock.unlock()
    }
    func stopObserving() {
        lock.lock()
        let wasObserving = observing
        observing = false
        result = nil
        cache.invalidate()
        lock.unlock()
        if wasObserving { PHPhotoLibrary.shared().unregisterChangeObserver(self) }
    }
    func photoLibraryDidChange(_ changeInstance: PHChange) {
        lock.lock()
        guard observing else { lock.unlock(); return }
        var changed = false
        if let result, let details = changeInstance.changeDetails(for: result) {
            self.result = details.fetchResultAfterChanges
            cache.invalidate()
            changed = true
        } else if result == nil { cache.invalidate(); changed = true }
        lock.unlock()
        if changed { onChange() }
    }
    private func snapshot(receiver: String, authorization: Int) -> (PHFetchResult<PHAsset>, DiscoveryCacheState.Ticket)? {
        lock.lock()
        guard observing, let ticket = cache.begin(context: .init(receiver: receiver, authorization: authorization),
                                                  now: Date().timeIntervalSince1970) else { lock.unlock(); return nil }
        lock.unlock()
        // Do not hold our notification lock while making a PhotoKit fetch.
        // Any notification during this fetch invalidates the reserved ticket.
        let fetched = PhotoSource.allAssets()
        lock.lock()
        guard observing else { lock.unlock(); return nil }
        result = fetched
        lock.unlock()
        return (fetched, ticket)
    }
    private func finish(_ ticket: DiscoveryCacheState.Ticket, succeeded: Bool) {
        lock.lock()
        defer { lock.unlock() }
        if succeeded { cache.finish(ticket, now: Date().timeIntervalSince1970) }
        else { cache.fail(ticket) }
    }
    func isCurrent(receiver: String, authorization: Int) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return observing && cache.isCurrent(context: .init(receiver: receiver, authorization: authorization),
                                             now: Date().timeIntervalSince1970)
    }
    func reconcile(queue: AssetSyncQueue, receiver: String, authorization: Int) async throws -> Int? {
        // Most small automatic batches need neither a fetch nor a detached task.
        if isCurrent(receiver: receiver, authorization: authorization) { return nil }
        // Scanning 100k metadata entries must not block SwiftUI's main actor.
        let job = Task.detached { [self] () async throws -> Int? in
            try Task.checkCancellation()
            guard let (snapshot, ticket) = self.snapshot(receiver: receiver, authorization: authorization) else { return nil }
            var succeeded = false
            defer { self.finish(ticket, succeeded: succeeded) }
            let generation = UUID().uuidString
            let now = Date().timeIntervalSince1970
            var batch: [QueueAsset] = []
            for index in 0..<snapshot.count {
                try Task.checkCancellation()
                batch.append(PhotoSource.queueAsset(snapshot.object(at: index)))
                if batch.count == 500 {
                    try await queue.discover(batch, receiver: receiver, generation: generation, now: now)
                    batch.removeAll(keepingCapacity: true)
                }
            }
            try await queue.discover(batch, receiver: receiver, generation: generation, now: now)
            try Task.checkCancellation()
            try await queue.finishDiscovery(receiver: receiver, generation: generation)
            try Task.checkCancellation()
            succeeded = true
            return snapshot.count
        }
        return try await withTaskCancellationHandler { try await job.value } onCancel: { job.cancel() }
    }
}

/// requestData supplies a cancellation ID, unlike writeData's completion-only API.
private final class ResourceExport: @unchecked Sendable {
    private let lock = NSRecursiveLock()
    private var continuation: CheckedContinuation<Void, Error>?
    private var terminal: Result<Void, Error>?
    private var requestID: PHAssetResourceDataRequestID?
    private let output: FileHandle

    init(url: URL) throws {
        guard FileManager.default.createFile(atPath: url.path, contents: nil) else {
            throw CocoaError(.fileWriteUnknown)
        }
        output = try FileHandle(forWritingTo: url)
    }

    private func finish(_ error: Error?) {
        lock.lock()
        guard terminal == nil else { lock.unlock(); return }
        var failure = error
        do { if failure == nil { try output.synchronize() }; try output.close() }
        catch { if failure == nil { failure = error } }
        let result: Result<Void, Error>
        if let failure { result = .failure(failure) }
        else { result = .success(Void()) }
        terminal = result
        let resume = continuation
        continuation = nil
        lock.unlock()
        resume?.resume(with: result)
    }

    func cancel() {
        finish(CancellationError())
        lock.lock()
        let request = requestID
        lock.unlock()
        if let request { PHAssetResourceManager.default().cancelDataRequest(request) }
    }

    func run(_ resource: PHAssetResource, options: PHAssetResourceRequestOptions) async throws {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (resume: CheckedContinuation<Void, Error>) in
                lock.lock()
                if let terminal { lock.unlock(); resume.resume(with: terminal); return }
                continuation = resume
                lock.unlock()
                let id = PHAssetResourceManager.default().requestData(for: resource, options: options) { data in
                    self.lock.lock()
                    defer { self.lock.unlock() }
                    guard self.terminal == nil else { return }
                    do { try self.output.write(contentsOf: data) }
                    catch { self.finish(error); self.cancel() }
                } completionHandler: { error in self.finish(error) }
                lock.lock()
                requestID = id
                let ended = terminal != nil
                lock.unlock()
                if ended { PHAssetResourceManager.default().cancelDataRequest(id) }
            }
        } onCancel: { self.cancel() }
    }
}

enum PhotoSource {
    static func queueAsset(_ asset: PHAsset) -> QueueAsset {
        let version = [String(asset.modificationDate?.timeIntervalSince1970 ?? 0),
            String(asset.creationDate?.timeIntervalSince1970 ?? 0), String(asset.mediaType.rawValue),
            String(asset.mediaSubtypes.rawValue), String(asset.pixelWidth), String(asset.pixelHeight),
            String(asset.duration)].joined(separator: "::")
        return QueueAsset(identifier: asset.localIdentifier, version: version,
                          changedAt: asset.modificationDate?.timeIntervalSince1970 ?? asset.creationDate?.timeIntervalSince1970 ?? 0)
    }
    static func requestReadAccess() async -> PHAuthorizationStatus {
        await PHPhotoLibrary.requestAuthorization(for: .readWrite)
    }

    static func allAssets() -> PHFetchResult<PHAsset> {
        let options = PHFetchOptions()
        options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        return PHAsset.fetchAssets(with: options)
    }

    static func resourceList(for asset: PHAsset) -> [PhotoResource] {
        let all = PHAssetResource.assetResources(for: asset)
        let hasOriginalPhoto = all.contains { $0.type == .photo }
        let hasOriginalVideo = all.contains { $0.type == .video }
        let formatter = ISO8601DateFormatter()
        return all.compactMap { item in
            let kind: String
            switch item.type {
            case .photo: kind = "photo"
            case .alternatePhoto: kind = "alternatePhoto"
            case .pairedVideo: kind = "pairedVideo"
            case .video: kind = "video"
            case .fullSizePhoto where !hasOriginalPhoto: kind = "fullSizePhoto"
            case .fullSizeVideo where !hasOriginalVideo: kind = "fullSizeVideo"
            default: return nil
            }
            let timestamp = String(asset.modificationDate?.timeIntervalSince1970 ?? 0)
            let key = [asset.localIdentifier, timestamp, kind, item.originalFilename].joined(separator: "::")
            return PhotoResource(assetKey: key, assetIdentifier: asset.localIdentifier, kind: kind,
                filename: item.originalFilename,
                createdAt: asset.creationDate.map { formatter.string(from: $0) },
                modifiedAt: asset.modificationDate.map { formatter.string(from: $0) }, resource: item)
        }
    }

    private static var staging: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("resource-staging", isDirectory: true)
    }

    static func cleanAbandonedExports() throws {
        // Called only before a serialized pass, after old URLSession tasks settle.
        guard FileManager.default.fileExists(atPath: staging.path) else { return }
        for url in try FileManager.default.contentsOfDirectory(at: staging, includingPropertiesForKeys: nil) {
            if UUID(uuidString: url.deletingPathExtension().lastPathComponent) != nil {
                try FileManager.default.removeItem(at: url)
            }
        }
    }

    static func export(_ item: PhotoResource, wifiOnly: Bool) async throws -> ExportedPhotoResource {
        try Task.checkCancellation()
        try FileManager.default.createDirectory(at: staging, withIntermediateDirectories: true)
        let output = staging.appendingPathComponent(UUID().uuidString + ".partial")
        var keep = false
        defer { if !keep { try? FileManager.default.removeItem(at: output) } }
        let options = PHAssetResourceRequestOptions()
        // PhotoKit has no per-request cellular prohibition. Strict Wi-Fi mode
        // therefore reads local originals only; the UI explains iCloud retrieval.
        options.isNetworkAccessAllowed = !wifiOnly
        let operation = try ResourceExport(url: output)
        try await operation.run(item.resource, options: options)
        try Task.checkCancellation()
        let stat = try FileManager.default.attributesOfItem(atPath: output.path)
        let size = (stat[.size] as? NSNumber)?.int64Value ?? 0
        guard size > 0 else { throw CocoaError(.fileReadCorruptFile) }
        var hasher = SHA256()
        let stream = try FileHandle(forReadingFrom: output)
        defer { try? stream.close() }
        while true {
            try Task.checkCancellation()
            let block = try stream.read(upToCount: 1024 * 1024) ?? Data()
            if block.isEmpty { break }
            hasher.update(data: block)
        }
        keep = true
        return ExportedPhotoResource(fileURL: output, size: size,
            sha256: hasher.finalize().map { String(format: "%02x", $0) }.joined())
    }
}
