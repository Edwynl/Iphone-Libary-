// swift-tools-version: 5.9
import PackageDescription

// Pure queue tests run without PhotoKit or signing on a Mac. The app itself
// remains an XcodeGen iOS target; passing these tests is not a device acceptance.
let package = Package(
    name: "PhotosSyncQueue",
    platforms: [.macOS(.v13), .iOS(.v17)],
    targets: [
        .target(name: "PhotosSyncQueue", path: "Sources", exclude: [
            "BackgroundChunkUploader.swift", "BackgroundScheduler.swift", "CancellationGate.swift",
            "CheckpointStore.swift", "ContentView.swift", "Info.plist", "KeychainStore.swift",
            "PhotoSource.swift", "PhotosCompanionApp.swift", "SyncAPI.swift", "SyncState.swift"
        ], sources: ["AssetSyncQueue.swift", "DiscoveryCacheState.swift"], linkerSettings: [.linkedLibrary("sqlite3")]),
        .testTarget(name: "PhotosSyncQueueTests", dependencies: ["PhotosSyncQueue"], path: "QueueTests")
    ]
)
