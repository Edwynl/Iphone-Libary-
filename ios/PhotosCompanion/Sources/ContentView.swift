import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var sync: SyncState
    @State private var pairingToken = ""
    @State private var showDisconnectConfirm = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Label("Original quality · Local-first", systemImage: "photo.stack")
                        .font(.headline)
                    Text("HEIC, Live Photo still + paired MOV, and original videos are transferred without re-encoding. Your iPhone originals are never deleted.")
                        .font(.footnote).foregroundStyle(.secondary)
                }

                Section("Windows computer") {
                    TextField("https://your-windows.tailnet.ts.net", text: $sync.serverURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    if sync.isPaired {
                        Label("Paired securely", systemImage: "checkmark.shield.fill")
                            .foregroundStyle(.green)
                        Button("Disconnect this device") { showDisconnectConfirm = true }
                            .foregroundStyle(.red)
                    } else {
                        SecureField("Pairing code shown in Windows", text: $pairingToken)
                        Button("Pair with Windows") {
                            let code = pairingToken
                            pairingToken = ""
                            Task { await sync.pair(masterToken: code) }
                        }
                        .disabled(sync.serverURL.isEmpty || pairingToken.isEmpty)
                    }
                    Text("Requires Windows AI Photos Sync Service running over private Tailscale Serve HTTPS. Never use an open public port.")
                        .font(.caption).foregroundStyle(.secondary)
                }

                Section("Automatic backup") {
                    Toggle("Allow automatic background sync", isOn: $sync.automatic)
                        .disabled(!sync.isPaired)
                    Toggle("Wi-Fi only", isOn: $sync.wifiOnly)
                    Text("Strict Wi-Fi mode reads local originals only because PhotoKit cannot prohibit cellular access per download. For iCloud-only originals, download them in Photos first, or turn this off to allow system iCloud retrieval and cellular transfer.")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("iOS schedules background opportunities; syncing may pause while the app is closed. Force quitting disables automatic relaunch. Open this app to complete an initial large library transfer.")
                        .font(.caption).foregroundStyle(.secondary)
                }

                Section("Sync progress") {
                    LabeledContent("Photo library", value: "\(sync.totalAssets) assets")
                    LabeledContent("Checked this pass", value: "\(sync.processedAssets)")
                    LabeledContent("Uploaded originals", value: "\(sync.uploadedResources)")
                    Text(sync.progress).font(.subheadline)
                    if !sync.lastError.isEmpty {
                        Text(sync.lastError).font(.footnote).foregroundStyle(.red)
                            .accessibilityIdentifier("sync-error")
                    }
                    if let date = sync.lastCompleted {
                        LabeledContent("Last successful pass") { Text(date, style: .date) }
                    }
                    Button {
                        Task { await sync.syncNow() }
                    } label: {
                        Label(sync.isRunning ? "Sync in progress" : "Sync all remaining photos",
                              systemImage: "arrow.triangle.2.circlepath")
                    }
                    .disabled(!sync.isPaired || sync.isRunning)
                }
            }
            .navigationTitle("Photos Companion")
            .confirmationDialog("Disconnect this iPhone?", isPresented: $showDisconnectConfirm) {
                Button("Disconnect", role: .destructive) { sync.disconnect() }
            } message: {
                Text("Windows device access is not revoked until you click Revoke on the Windows Sync Management screen.")
            }
        }
    }
}
