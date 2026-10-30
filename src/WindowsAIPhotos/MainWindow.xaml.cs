using System.Collections.ObjectModel;
using System.Linq;
using System.Diagnostics;
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Windows.Storage.Pickers;
using Windows.System;
using WindowsAIPhotos.Models;
using WindowsAIPhotos.Services;

namespace WindowsAIPhotos;

public sealed partial class MainWindow : Window
{
    private const int PageSize = 120;
    private readonly ObservableCollection<PhotoCard> _photos = new();
    private readonly ObservableCollection<string> _folders = new();
    private PythonEngineBridge? _engine;
    private bool _started;
    private bool _working;
    private bool _isSearch;
    private int _offset;

    public MainWindow()
    {
        InitializeComponent();
        PhotoGrid.ItemsSource = _photos;
        FolderList.ItemsSource = _folders;
        Closed += (_, _) => _engine?.Dispose();
    }

    private async void Root_Loaded(object sender, RoutedEventArgs e)
    {
        if (_started) return;
        _started = true;
        try
        {
            _engine = new PythonEngineBridge();
            _engine.Progress += progress => DispatcherQueue.TryEnqueue(() => HandleProgress(progress));
            await _engine.StartAsync();
            await RefreshStatusAsync();
            await RefreshFoldersAsync();
            await BrowseAsync(reset: true);
            await RefreshSyncSidebarAsync();
        }
        catch (Exception ex) { ShowError("Cannot start local engine", ex); }
    }

    private async Task<JsonElement> CallAsync(string action, object? parameters = null)
    {
        if (_engine is null) throw new InvalidOperationException("Engine not ready");
        return await _engine.RequestAsync(action, parameters);
    }

    private async Task RefreshStatusAsync()
    {
        var data = await CallAsync("status");
        int count = data.GetProperty("photos").GetInt32();
        int embedded = data.GetProperty("embeddings").GetInt32();
        bool installed = data.GetProperty("ai_installed").GetBoolean();
        PhotoCount.Text = $"{count:N0} photos";
        AiState.Text = installed ? $"Ready · {embedded:N0}/{count:N0} AI-indexed"
                                 : "Not installed · filename search only";
        AiButton.IsEnabled = !installed && !_working;
        AiButton.Visibility = installed ? Visibility.Collapsed : Visibility.Visible;
    }

    private async Task RefreshFoldersAsync()
    {
        _folders.Clear();
        var response = await CallAsync("folders");
        foreach (var item in response.GetProperty("folders").EnumerateArray())
            _folders.Add(item.GetString() ?? "");
    }

    private static string Str(JsonElement row, string key) =>
        row.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? "" : "";

    private void DisplayPhotos(JsonElement json, bool append)
    {
        if (!append) _photos.Clear();
        foreach (var row in json.EnumerateArray())
        {
            var path = Str(row, "path");
            if (path.Length == 0) continue;
            var date = row.TryGetProperty("mtime_ns", out var element) && element.TryGetInt64(out var ns)
                ? ns : 0;
            _photos.Add(PhotoCard.Create(path, Str(row, "name"), Str(row, "thumb_path"), date));
        }
        EmptyState.Visibility = _photos.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        EmptyState.Text = _folders.Count == 0 ? "Choose Add photo folder to begin."
            : _isSearch ? "No matching photos found." : "No photos indexed yet. Choose Scan.";
        LoadMoreButton.Visibility = !_isSearch && json.GetArrayLength() == PageSize
            ? Visibility.Visible : Visibility.Collapsed;
    }

    private async Task BrowseAsync(bool reset)
    {
        _isSearch = false;
        if (reset) _offset = 0;
        var result = await CallAsync("browse", new { limit = PageSize, offset = _offset });
        DisplayPhotos(result.GetProperty("photos"), append: !reset);
        PageTitle.Text = "All Photos";
        StatusText.Text = _photos.Count == 0 ? "Add a local folder to start scanning."
            : $"Showing {_photos.Count:N0} local photos. Select an image to open its original file.";
    }

    private async Task SearchAsync()
    {
        string query = SearchBox.Text.Trim();
        if (query.Length == 0) { await BrowseAsync(true); return; }
        _isSearch = true;
        StatusText.Text = "Searching local photo library...";
        var result = await CallAsync("search", new { query, limit = 200 });
        DisplayPhotos(result.GetProperty("photos"), append: false);
        string mode = Str(result, "mode");
        PageTitle.Text = "Search Results";
        StatusText.Text = mode == "semantic"
            ? $"AI semantic search · {_photos.Count} results for \"{query}\""
            : $"Filename search only · {_photos.Count} results · Install AI for image-content search";
        if (result.TryGetProperty("warning", out var warning) && warning.ValueKind == JsonValueKind.String)
            StatusText.Text += " · " + warning.GetString();
    }

    private async Task ScanAsync()
    {
        if (_working) return;
        SetBusy(true);
        try
        {
            StatusText.Text = "Indexing photos... Original files will not be modified.";
            var result = await CallAsync("scan");
            int count = result.GetProperty("seen").GetInt32();
            int changed = result.GetProperty("updated").GetInt32();
            int embedded = result.GetProperty("embedded").GetInt32();
            await RefreshStatusAsync();
            await BrowseAsync(true);
            StatusText.Text = $"Scan complete: {count:N0} found, {changed:N0} updated, {embedded:N0} AI indexed.";
        }
        catch (Exception ex) { ShowError("Scan failed", ex); }
        finally { SetBusy(false); }
    }

    private void SetBusy(bool busy)
    {
        _working = busy;
        WorkProgress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        ScanButton.IsEnabled = !busy;
        AddFolderButton.IsEnabled = !busy;
        RemoveFolderButton.IsEnabled = !busy;
        AiButton.IsEnabled = !busy && AiButton.Visibility == Visibility.Visible;
        DuplicatesButton.IsEnabled = !busy;
        OrganizeButton.IsEnabled = !busy;
        MobileButton.IsEnabled = !busy;
        ManageSyncButton.IsEnabled = !busy;
        BackupButton.IsEnabled = !busy;
        CloudButton.IsEnabled = !busy;
    }

    private void HandleProgress(JsonElement payload)
    {
        var stage = Str(payload, "stage");
        if (stage == "indexing")
        {
            int count = payload.TryGetProperty("seen", out var seen) ? seen.GetInt32() : 0;
            int ai = payload.TryGetProperty("embedded", out var embeds) ? embeds.GetInt32() : 0;
            StatusText.Text = $"Scanning {count:N0} photos · {ai:N0} AI indexed";
        }
        else if (stage == "installing")
            StatusText.Text = "Downloading AI model to your computer (one-time operation)...";
        else if (stage == "warning")
            StatusText.Text = Str(payload, "message");
    }

    private async void AddFolderButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var picker = new FolderPicker();
            picker.FileTypeFilter.Add("*");
            WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
            var result = await picker.PickSingleFolderAsync();
            if (result is null) return;
            await CallAsync("add_folder", new { path = result.Path });
            await RefreshFoldersAsync();
            await ScanAsync();
        }
        catch (Exception ex) { ShowError("Cannot add folder", ex); }
    }

    private async void RemoveFolderButton_Click(object sender, RoutedEventArgs e)
    {
        if (_working || FolderList.SelectedItem is not string path) return;
        try
        {
            var confirm = new ContentDialog
            {
                Title = "Remove this folder from the photo library?",
                Content = "The database entries and search indexes for this folder will be forgotten. Original photos will NEVER be deleted or modified.",
                PrimaryButtonText = "Remove from library",
                CloseButtonText = "Cancel",
                DefaultButton = ContentDialogButton.Close,
                XamlRoot = Root.XamlRoot
            };
            if (await confirm.ShowAsync() != ContentDialogResult.Primary) return;
            await CallAsync("remove_folder", new { path });
            await RefreshFoldersAsync();
            await RefreshStatusAsync();
            await BrowseAsync(true);
            StatusText.Text = "Folder removed from the catalog. Original photos remain untouched.";
        }
        catch (Exception ex) { ShowError("Cannot remove folder", ex); }
    }

    private async void AiButton_Click(object sender, RoutedEventArgs e)
    {
        if (_working) return;
        SetBusy(true);
        try
        {
            // User-initiated download; never fetch model weights silently.
            await CallAsync("install_model");
            await RefreshStatusAsync();
            StatusText.Text = "AI model installed. Choose Scan to create semantic indexes.";
        }
        catch (Exception ex) { ShowError("AI installation failed", ex); }
        finally { SetBusy(false); }
    }

    private async Task ShowMessageAsync(string title, string body)
    {
        var dialog = new ContentDialog
        {
            Title = title,
            Content = new ScrollViewer
            {
                Content = new TextBlock { Text = body, TextWrapping = TextWrapping.Wrap },
                MaxHeight = 420
            },
            CloseButtonText = "Close",
            XamlRoot = Root.XamlRoot
        };
        await dialog.ShowAsync();
    }

    private async void OrganizeButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var data = await CallAsync("organize");
            var entries = data.GetProperty("timeline");
            var lines = entries.EnumerateArray().Take(24).Select(p =>
                $"{Str(p, "month")}: {p.GetProperty("count").GetInt32():N0} photos");
            await ShowMessageAsync("Virtual photo timeline",
                "Grouped by EXIF capture month, or file date where unavailable." +
                Environment.NewLine + Environment.NewLine + string.Join(Environment.NewLine, lines) +
                Environment.NewLine + Environment.NewLine +
                "Virtual albums do not move or rename source photographs.");
        }
        catch (Exception ex) { ShowError("Could not organize photos", ex); }
    }

    private async void DuplicatesButton_Click(object sender, RoutedEventArgs e)
    {
        if (_working) return;
        SetBusy(true);
        try
        {
            StatusText.Text = "Checking exact and visually similar photos. Originals are never deleted.";
            var data = await CallAsync("duplicates", new { distance = 4 });
            var exact = data.GetProperty("exact_groups");
            var similar = data.GetProperty("similar_groups");
            var lines = new System.Collections.Generic.List<string>
            {
                $"Exact duplicate groups (SHA-256): {exact.GetArrayLength()}",
                $"Visually similar groups (review only): {similar.GetArrayLength()}",
                "", "No automatic deletion is performed."
            };
            int shown = 0;
            foreach (var group in exact.EnumerateArray())
            {
                if (shown++ >= 8) break;
                lines.Add("- " + string.Join(" | ", group.EnumerateArray().Select(p => Str(p, "name"))));
            }
            await ShowMessageAsync("Duplicate photo review", string.Join(Environment.NewLine, lines));
            StatusText.Text = "Duplicate analysis finished. Photo originals remain untouched.";
        }
        catch (Exception ex) { ShowError("Duplicate scan failed", ex); }
        finally { SetBusy(false); }
    }

    private async void MobileButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var data = await CallAsync("start_mobile", new { port = 8787 });
            var token = Str(data, "pairing_token");
            var command = Str(data, "tailscale_command");
            await ShowMessageAsync("iPhone companion enabled",
                "Local: http://127.0.0.1:8787" + Environment.NewLine +
                "From iPhone away from home: run this on Windows (Tailscale installed):" +
                Environment.NewLine + command + Environment.NewLine + Environment.NewLine +
                "Open the private HTTPS URL shown by Tailscale Serve on your iPhone." +
                Environment.NewLine + Environment.NewLine +
                "Pairing token (keep private): " + token + Environment.NewLine + Environment.NewLine +
                "Install the iOS Photos Companion project on the iPhone, enter the private HTTPS URL and pairing token, then enable automatic sync. iOS schedules background opportunities; this is not real-time continuous sync.");
            await RefreshSyncSidebarAsync();
        }
        catch (Exception ex) { ShowError("Cannot enable iPhone companion", ex); }
    }

    private async void BackupButton_Click(object sender, RoutedEventArgs e)
    {
        if (_working) return;
        try
        {
            var picker = new FolderPicker();
            picker.FileTypeFilter.Add("*");
            WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
            var folder = await picker.PickSingleFolderAsync();
            if (folder is null) return;
            SetBusy(true);
            var planned = await CallAsync("backup_local", new { target = folder.Path, apply = false });
            var files = planned.GetProperty("files").GetInt32();
            var bytes = planned.GetProperty("bytes_planned").GetInt64();
            var dialog = new ContentDialog
            {
                Title = "Back up original photos?",
                Content = $"Destination: {folder.Path}\n{files:N0} indexed photos\nUp to {bytes / (1024.0 * 1024 * 1024):F2} GB planned.\nOnly copies. No deletion and no folder mirroring.",
                PrimaryButtonText = "Start backup",
                CloseButtonText = "Cancel",
                XamlRoot = Root.XamlRoot
            };
            if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
            var done = await CallAsync("backup_local", new { target = folder.Path, apply = true });
            await ShowMessageAsync("Backup finished", $"Copied: {done.GetProperty("copied").GetInt32()}\nUnchanged: {done.GetProperty("unchanged").GetInt32()}");
        }
        catch (Exception ex) { ShowError("Backup failed", ex); }
        finally { SetBusy(false); }
    }

    private async void CloudButton_Click(object sender, RoutedEventArgs e)
    {
        await ShowMessageAsync("Cloud backup (optional beta)",
            "S3-compatible backups are implemented in the local engine. " +
            "AWS S3, Backblaze B2, and Cloudflare R2 can be configured through CLI " +
            "credentials and HTTPS endpoints. This GUI does not collect or store cloud secrets yet." +
            Environment.NewLine + Environment.NewLine +
            "Start with: python -m ai_photos.cli backup-s3 --bucket YOUR_BUCKET" +
            Environment.NewLine +
            "Add --endpoint-url https://... and --apply after reviewing the dry run." +
            Environment.NewLine + Environment.NewLine +
            "Cloud backup sends original photos to your chosen provider. It is off by default; " +
            "client-side encryption and restore UI are not yet implemented.");
    }

    private async void ScanButton_Click(object sender, RoutedEventArgs e) => await ScanAsync();
    private async void LibraryButton_Click(object sender, RoutedEventArgs e)
    {
        try { SearchBox.Text = ""; await BrowseAsync(true); }
        catch (Exception ex) { ShowError("Cannot open library", ex); }
    }
    private async void SearchButton_Click(object sender, RoutedEventArgs e)
    {
        try { await SearchAsync(); }
        catch (Exception ex) { ShowError("Search failed", ex); }
    }
    private async void SearchBox_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key != VirtualKey.Enter) return;
        e.Handled = true;
        try { await SearchAsync(); }
        catch (Exception ex) { ShowError("Search failed", ex); }
    }
    private async void LoadMoreButton_Click(object sender, RoutedEventArgs e)
    {
        try { _offset = _photos.Count; await BrowseAsync(reset: false); }
        catch (Exception ex) { ShowError("Cannot load more photos", ex); }
    }
    private void PhotoGrid_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is not PhotoCard photo) return;
        try { Process.Start(new ProcessStartInfo(photo.Path) { UseShellExecute = true }); }
        catch (Exception ex) { ShowError("Cannot open original photo", ex); }
    }
    private void ShowError(string prefix, Exception ex)
    {
        StatusText.Text = $"{prefix}: {ex.Message}";
        Debug.WriteLine(ex);
    }
}
