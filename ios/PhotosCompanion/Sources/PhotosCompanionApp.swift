import SwiftUI

@main
struct PhotosCompanionApp: App {
    @UIApplicationDelegateAdaptor(PhotosAppDelegate.self) private var appDelegate
    @StateObject private var sync = SyncState.shared
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(sync)
                .onChange(of: phase) { _, current in
                    if current == .active { sync.librarySelectionMayHaveChanged() }
                    if current == .active && sync.automatic && sync.isPaired {
                        sync.scheduleBackground()
                        Task { await sync.automaticPass(maxAssets: 100) }
                    }
                }
        }
    }
}
