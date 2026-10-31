import Foundation
import CryptoKit

struct PairRequest: Encodable {
    let device_id: String
    let name: String
}
struct PairReply: Decodable {
    let device_id: String
    let device_token: String
    let receiver_id: String
    let protocolVersion: Int
    enum CodingKeys: String, CodingKey {
        case device_id, device_token, receiver_id
        case protocolVersion = "protocol"
    }
}
struct BeginRequest: Encodable {
    let asset_id: String
    let resource_type: String
    let filename: String
    let size: Int64
    let sha256: String
    let resource_key: String
    let asset_created_at: String?
    let asset_modified_at: String?
}
struct VerifyRequest: Encodable {
    let asset_id: String
    let resource_type: String
    let sha256: String
}
struct VerifyReply: Decodable { let complete: Bool; let receiver_id: String }

private final class NoRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
struct TransferReply: Decodable {
    let transfer_id: String
    let status: String
    let offset: Int64
    let size: Int64
    let chunk_bytes: Int?
}
struct OffsetReply: Decodable {
    let offset: Int64
}
struct CompleteReply: Decodable {
    let status: String
}
struct ServerErrorReply: Decodable {
    let error: String
    let expected_offset: Int64?
}

enum SyncAPIError: LocalizedError {
    case invalidURL
    case notPaired
    case http(Int, String)
    case invalidResponse

    var errorDescription: String? {
        switch self {
        case .invalidURL: return "Use your private Tailscale HTTPS URL (https://...ts.net)."
        case .notPaired: return "Pair the iPhone with Windows before syncing."
        case .http(let status, let message): return "Windows returned \(status): \(message)"
        case .invalidResponse: return "Windows returned an invalid transfer response."
        }
    }
}

struct SyncAPI {
    let base: URL
    let wifiOnly: Bool
    var origin: String { base.absoluteString }

    init(serverURL: String, wifiOnly: Bool) throws {
        guard let url = URL(string: serverURL.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.scheme?.lowercased() == "https", let host = url.host?.lowercased(),
              host.hasSuffix(".ts.net"), host.count > 7, (url.port == nil || url.port == 443),
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.path.isEmpty || url.path == "/" else { throw SyncAPIError.invalidURL }
        base = URL(string: "https://" + host)!
        self.wifiOnly = wifiOnly
    }

    private func endpoint(_ path: String) -> URL {
        base.appendingPathComponent(path.trimmingCharacters(in: CharacterSet(charactersIn: "/")))
    }

    private func makeRequest(_ path: String, method: String, token: String, data: Data? = nil,
                             timeout: TimeInterval = 90) -> URLRequest {
        var request = URLRequest(url: endpoint(path))
        request.httpMethod = method
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = timeout
        request.allowsExpensiveNetworkAccess = !wifiOnly
        request.allowsCellularAccess = !wifiOnly
        request.allowsConstrainedNetworkAccess = !wifiOnly
        if data != nil {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = data
        }
        return request
    }

    private func send<T: Decodable>(_ path: String, method: String, token: String,
                                    payload: Data? = nil, timeout: TimeInterval = 90) async throws -> T {
        let request = makeRequest(path, method: method, token: token, data: payload, timeout: timeout)
        let session = URLSession(configuration: .ephemeral, delegate: NoRedirects(), delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        let (data, response) = try await session.data(for: request)
        guard let code = (response as? HTTPURLResponse)?.statusCode else { throw SyncAPIError.invalidResponse }
        guard (200...299).contains(code) else {
            let server = try? JSONDecoder().decode(ServerErrorReply.self, from: data)
            throw SyncAPIError.http(code, server?.error ?? "Request failed")
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    func pair(masterToken: String, deviceId: String, deviceName: String) async throws -> PairReply {
        let body = try JSONEncoder().encode(PairRequest(device_id: deviceId, name: deviceName))
        let reply: PairReply = try await send("api/sync/v1/pair", method: "POST", token: masterToken, payload: body)
        guard reply.protocolVersion == 1 else { throw SyncAPIError.invalidResponse }
        return reply
    }

    func verify(_ item: PhotoResource, checkpoint: ResourceCheckpoint, receiver: String, token: String) async throws -> Bool {
        let body = try JSONEncoder().encode(VerifyRequest(asset_id: item.assetIdentifier,
            resource_type: item.kind, sha256: checkpoint.sha256))
        let reply: VerifyReply = try await send("api/sync/v1/verify", method: "POST", token: token, payload: body)
        guard reply.receiver_id == receiver else { throw SyncAPIError.notPaired }
        return reply.complete
    }

    func upload(_ item: PhotoResource, exported: ExportedPhotoResource, deviceToken: String) async throws {
        let request = BeginRequest(asset_id: item.assetIdentifier, resource_type: item.kind,
                                   filename: item.filename, size: exported.size, sha256: exported.sha256,
                                   resource_key: item.assetKey, asset_created_at: item.createdAt,
                                   asset_modified_at: item.modifiedAt)
        let body = try JSONEncoder().encode(request)
        var state: TransferReply = try await send("api/sync/v1/transfers", method: "POST",
                                                   token: deviceToken, payload: body)
        if state.status == "complete" { return }
        guard state.size == exported.size, state.offset >= 0, state.offset <= exported.size,
              (state.chunk_bytes ?? 0) > 0, (state.chunk_bytes ?? 0) <= 8 * 1024 * 1024 else {
            throw SyncAPIError.invalidResponse
        }
        try await BackgroundChunkUploader.shared.waitForPreviousTasks()
        let handle = try FileHandle(forReadingFrom: exported.fileURL)
        defer { try? handle.close() }
        var offset = state.offset
        while offset < exported.size {
            try Task.checkCancellation()
            try handle.seek(toOffset: UInt64(offset))
            let requested = min(Int64(state.chunk_bytes!), exported.size - offset)
            let bytes = try handle.read(upToCount: Int(requested)) ?? Data()
            guard bytes.count == Int(requested) else { throw SyncAPIError.invalidResponse }
            let chunkHash = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
            var chunkRequest = makeRequest("api/sync/v1/transfers/\(state.transfer_id)/chunk",
                                            method: "PUT", token: deviceToken)
            chunkRequest.setValue(String(offset), forHTTPHeaderField: "X-Upload-Offset")
            chunkRequest.setValue(chunkHash, forHTTPHeaderField: "X-Chunk-SHA256")
            chunkRequest.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
            let response = try await BackgroundChunkUploader.shared.upload(request: chunkRequest, bytes: bytes)
            if response.status == 409 {
                // Retry only after consulting Windows; a lost acknowledgement might
                // mean the previous chunk was already committed.
                let fresh: TransferReply = try await send("api/sync/v1/transfers/\(state.transfer_id)",
                                                          method: "GET", token: deviceToken)
                guard fresh.size == exported.size, fresh.offset >= 0, fresh.offset <= exported.size,
                      (fresh.chunk_bytes ?? 0) > 0, (fresh.chunk_bytes ?? 0) <= 8 * 1024 * 1024 else { throw SyncAPIError.invalidResponse }
                if fresh.status == "complete" { return }
                // A cancelled/failed transfer never accepts chunks again; the next
                // pass begins a fresh transfer. An unchanged offset would loop.
                guard fresh.status == "pending", fresh.offset != offset else {
                    throw SyncAPIError.http(409, "Transfer is no longer pending; it restarts on the next pass")
                }
                offset = fresh.offset
                state = fresh
                continue
            }
            guard response.status == 200 else {
                let error = try? JSONDecoder().decode(ServerErrorReply.self, from: response.body)
                throw SyncAPIError.http(response.status, error?.error ?? "Chunk failed")
            }
            let reply = try JSONDecoder().decode(OffsetReply.self, from: response.body)
            guard reply.offset == offset + Int64(bytes.count) else { throw SyncAPIError.invalidResponse }
            offset = reply.offset
        }
        // Windows copies and hashes the whole resource before replying; a large
        // video can take minutes with no bytes on the wire.
        let done: CompleteReply = try await send("api/sync/v1/transfers/\(state.transfer_id)/complete",
                                                   method: "POST", token: deviceToken, timeout: 30 * 60)
        guard done.status == "complete" else { throw SyncAPIError.invalidResponse }
    }
}
