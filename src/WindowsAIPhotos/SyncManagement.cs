using System;
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace WindowsAIPhotos;

public sealed partial class MainWindow
{
    private static string FormatBytes(long count)
    {
        double size = count;
        foreach (var unit in new[] { "B", "KB", "MB", "GB", "TB" })
        {
            if (size < 1024 || unit == "TB") return $"{size:F1} {unit}";
            size /= 1024;
        }
        return "0 B";
    }

    private async Task RefreshSyncSidebarAsync()
    {
        try
        {
            var result = await CallAsync("sync_dashboard");
            var total = result.GetProperty("totals");
            int paired = 0;
            foreach (var device in result.GetProperty("devices").EnumerateArray())
                if (device.GetProperty("revoked_at").ValueKind == JsonValueKind.Null) paired++;
            bool enabled = result.GetProperty("server_running").GetBoolean();
            bool paused = result.GetProperty("paused").GetBoolean();
            int complete = total.GetProperty("completed").GetInt32();
            SyncStatusText.Text = $"iPhone: {(enabled ? paused ? "Paused" : "Listening" : "Off")} · " +
                                  $"{paired} paired · {complete:N0} backed-up resources";
        }
        catch (Exception ex)
        {
            SyncStatusText.Text = "Sync status unavailable: " + ex.Message;
        }
    }

    private TextBlock SyncLine(string text, bool heading = false)
    {
        return new TextBlock
        {
            Text = text, TextWrapping = TextWrapping.Wrap,
            FontSize = heading ? 15 : 12,
            FontWeight = heading ? Microsoft.UI.Text.FontWeights.SemiBold : Microsoft.UI.Text.FontWeights.Normal
        };
    }

    private async void ManageSyncButton_Click(object sender, RoutedEventArgs args)
    {
        var body = new StackPanel { Spacing = 12, MinWidth = 410 };
        var warning = SyncLine("Uploads are disabled until you choose Enable iPhone pairing. Originals are never deleted.");
        var controls = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        var refreshButton = new Button { Content = "Refresh" };
        var pauseButton = new Button { Content = "Pause" };
        var stopButton = new Button { Content = "Stop receiver" };
        controls.Children.Add(refreshButton);
        controls.Children.Add(pauseButton);
        controls.Children.Add(stopButton);
        var details = new StackPanel { Spacing = 10 };
        bool loading = false;
        bool confirmingAction = false;
        body.Children.Add(warning);
        body.Children.Add(controls);
        body.Children.Add(details);

        async Task LoadAsync()
        {
            if (loading) return;
            loading = true;
            details.Children.Clear();
            try
            {
                var data = await CallAsync("sync_dashboard");
                bool paused = data.GetProperty("paused").GetBoolean();
                bool serverRunning = data.GetProperty("server_running").GetBoolean();
                var totals = data.GetProperty("totals");
                pauseButton.Content = paused ? "Resume transfers" : "Pause transfers";
                stopButton.IsEnabled = serverRunning;
                details.Children.Add(SyncLine(serverRunning
                    ? $"Listening locally on 127.0.0.1:{data.GetProperty("server_port").GetInt32()}" +
                      " · Tailscale Serve HTTPS required for iPhone"
                    : "Server off — use Enable iPhone pairing to start"));
                if (serverRunning) details.Children.Add(SyncLine("Background receiver stays active after this window closes. Stop receiver ends it; there is no automatic start after sign-out/reboot."));
                if (data.TryGetProperty("receiver_error", out var receiverError) && receiverError.ValueKind == JsonValueKind.String)
                    warning.Text = "Receiver status unavailable: " + receiverError.GetString();
                details.Children.Add(SyncLine($"Completed: {totals.GetProperty("completed").GetInt32():N0} resources" +
                    $" · Incomplete: {totals.GetProperty("pending").GetInt32():N0}" +
                    $" · Received: {FormatBytes(totals.GetProperty("bytes_received").GetInt64())}"));
                details.Children.Add(SyncLine("Paired devices", heading: true));
                var devices = data.GetProperty("devices");
                if (devices.GetArrayLength() == 0)
                    details.Children.Add(SyncLine("No iPhones paired. Start server and use the pairing code in the native iOS app."));
                foreach (var item in devices.EnumerateArray())
                {
                    string deviceId = Str(item, "device_id");
                    string name = Str(item, "name");
                    string lastSeen = Str(item, "last_seen_at");
                    bool revoked = item.GetProperty("revoked_at").ValueKind != JsonValueKind.Null;
                    var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
                    row.Children.Add(new TextBlock
                    {
                        Text = $"{name} {(revoked ? "[REVOKED]" : "")}" + Environment.NewLine +
                               $"{item.GetProperty("completed").GetInt32()} completed · " +
                               $"{item.GetProperty("pending").GetInt32()} pending" + Environment.NewLine +
                               $"Last seen: {lastSeen}",
                        Width = 290, TextWrapping = TextWrapping.Wrap, FontSize = 12
                    });
                    if (!revoked)
                    {
                        var revoke = new Button { Content = "Revoke" };
                        bool confirm = false;
                        revoke.Click += async (_, _) =>
                        {
                            if (!confirm)
                            {
                                confirm = true;
                                confirmingAction = true;
                                revoke.Content = "Confirm revoke";
                                warning.Text = "Click Confirm revoke again to invalidate this iPhone token.";
                                return;
                            }
                            try
                            {
                                await CallAsync("sync_revoke_device", new { device_id = deviceId });
                                confirmingAction = false;
                                warning.Text = "Device revoked; reconnect on iPhone requires a new pairing code.";
                                await LoadAsync();
                                await RefreshSyncSidebarAsync();
                            }
                            catch (Exception ex) { warning.Text = ex.Message; }
                        };
                        row.Children.Add(revoke);
                    }
                    details.Children.Add(row);
                }
                details.Children.Add(SyncLine("Recent resource transfers", heading: true));
                var recent = data.GetProperty("recent_transfers");
                if (recent.GetArrayLength() == 0) details.Children.Add(SyncLine("No transfers yet"));
                foreach (var transfer in recent.EnumerateArray())
                {
                    long size = transfer.GetProperty("size").GetInt64();
                    long offset = transfer.GetProperty("offset").GetInt64();
                    int percent = size == 0 ? 0 : (int)Math.Min(100, offset * 100.0 / size);
                    details.Children.Add(SyncLine($"{Str(transfer, "filename")} · " +
                        $"{Str(transfer, "resource_type")} · {Str(transfer, "status")} · {percent}%"));
                    if (Str(transfer, "status") != "complete")
                    {
                        var cancel = new Button { Content = "Discard incomplete staging (click twice)" };
                        bool confirmed = false;
                        string deviceId = Str(transfer, "device_id"), transferId = Str(transfer, "id");
                        cancel.Click += async (_, _) =>
                        {
                            if (!confirmed) { confirmed = true; confirmingAction = true; cancel.Content = "Confirm discard — iPhone original stays untouched"; return; }
                            try { await CallAsync("sync_cancel_transfer", new { device_id = deviceId, transfer_id = transferId }); confirmingAction = false; await LoadAsync(); }
                            catch (Exception ex) { warning.Text = ex.Message; }
                        };
                        details.Children.Add(cancel);
                    }
                }
            }
            catch (Exception ex) { warning.Text = "Unable to load sync status: " + ex.Message; }
            finally { loading = false; }
        }

        refreshButton.Click += async (_, _) => { confirmingAction = false; await LoadAsync(); };
        pauseButton.Click += async (_, _) =>
        {
            try
            {
                var state = await CallAsync("sync_dashboard");
                await CallAsync("sync_pause", new { paused = !state.GetProperty("paused").GetBoolean() });
                await LoadAsync();
                await RefreshSyncSidebarAsync();
            }
            catch (Exception ex) { warning.Text = ex.Message; }
        };
        stopButton.Click += async (_, _) =>
        {
            try
            {
                await CallAsync("stop_mobile");
                await LoadAsync();
                await RefreshSyncSidebarAsync();
            }
            catch (Exception ex) { warning.Text = ex.Message; }
        };
        var dialog = new ContentDialog
        {
            Title = "iPhone Sync Management / 设备同步管理",
            Content = new ScrollViewer
            {
                Content = body,
                MinHeight = 180,
                MaxHeight = 490,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
            },
            CloseButtonText = "Close",
            XamlRoot = Root.XamlRoot
        };
        await LoadAsync();
        var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
        bool refreshing = false;
        timer.Tick += async (_, _) =>
        {
            if (refreshing) return;
            refreshing = true;
            try { if (!confirmingAction) await LoadAsync(); await RefreshSyncSidebarAsync(); }
            finally { refreshing = false; }
        };
        timer.Start();
        try { await dialog.ShowAsync(); }
        finally { timer.Stop(); }
    }
}
