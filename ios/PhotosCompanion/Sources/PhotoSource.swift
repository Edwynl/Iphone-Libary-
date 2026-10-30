import Foundation
import Photos
import CryptoKit

struct PhotoResource {
    let assetKey: String
    let assetIdentifier: String
    let kind: String
    let filename: String
    let resource: PHAssetResource
}

struct ExportedPhotoResource {
    let fileURL: URL
    let size: Int64
    let sha256: String
}

enum PhotoSource {
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
        return all.compactMap { item in
            let kind: String
            switch item.type {
            case .photo: kind = "photo"
            case .pairedVideo: kind = "pairedVideo"
            case .video: kind = "video"
            case .fullSizePhoto where !hasOriginalPhoto: kind = "fullSizePhoto"
            case .fullSizeVideo where !hasOriginalVideo: kind = "fullSizeVideo"
            default: return nil // Do not mistake adjustment metadata for originals.
            }
            let timestamp = String(asset.modificationDate?.timeIntervalSince1970 ?? 0)
            let key = [asset.localIdentifier, timestamp, kind, item.originalFilename].joined(separator: "::")
            return PhotoResource(assetKey: key, assetIdentifier: asset.localIdentifier,
                                 kind: kind, filename: item.originalFilename, resource: item)
        }
    }

    static func export(_ item: PhotoResource) async throws -> ExportedPhotoResource {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let stage = base.appendingPathComponent("resource-staging", isDirectory: true)
        try FileManager.default.createDirectory(at: stage, withIntermediateDirectories: true)
        let extensionName = URL(fileURLWithPath: item.filename).pathExtension.lowercased()
        let output = stage.appendingPathComponent(UUID().uuidString + "." + extensionName)
        let options = PHAssetResourceRequestOptions()
        options.isNetworkAccessAllowed = true // Original might be only in iCloud; Photos permission required.
        do {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                PHAssetResourceManager.default().writeData(for: item.resource, toFile: output,
                                                           options: options) { error in
                    if let error { continuation.resume(throwing: error) }
                    else { continuation.resume() }
                }
            }
        } catch {
            try? FileManager.default.removeItem(at: output)
            throw error
        }
        let stat = try FileManager.default.attributesOfItem(atPath: output.path)
        let size = (stat[.size] as? NSNumber)?.int64Value ?? 0
        guard size > 0 else {
            try? FileManager.default.removeItem(at: output)
            throw NSError(domain: "PhotoExport", code: 1, userInfo: [NSLocalizedDescriptionKey: "Empty PhotoKit resource"])
        }
        var hasher = SHA256()
        let stream = try FileHandle(forReadingFrom: output)
        defer { try? stream.close() }
        while true {
            let block = try stream.read(upToCount: 1024 * 1024) ?? Data()
            if block.isEmpty { break }
            hasher.update(data: block)
        }
        return ExportedPhotoResource(fileURL: output, size: size,
                                     sha256: hasher.finalize().map { String(format: "%02x", $0) }.joined())
    }
}
