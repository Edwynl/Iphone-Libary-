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
    private bool _selecting;
    private string MediaType => MediaFilter.SelectedIndex == 1 ? "photo" : MediaFilter.SelectedIndex == 2 ? "video" : "all";

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
        int videos = data.TryGetProperty("videos", out var v) ? v.GetInt32() : 0;
        int live = data.TryGetProperty("live_photos", out var l) ? l.GetInt32() : 0;
        PhotoCount.Text = $"{count:N0} 张照片（{live:N0} 实况） · {videos:N0} 个视频";
        AiState.Text = installed ? $"Ready · {embedded:N0}/{count + videos:N0} AI-indexed"
                                 : "Not installed · filename search only";
        if (data.TryGetProperty("ai_error", out var aiError) && !string.IsNullOrEmpty(aiError.GetString()))
            AiState.Text = "AI unavailable — repair/install: " + aiError.GetString();
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
            var card = PhotoCard.Create(path, Str(row, "name"), Str(row, "thumb_path"), date, Str(row, "taken_at"),
                Str(row, "media_kind"), Str(row, "companion"));
            _photos.Add(card);
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
        var result = await CallAsync("browse", new { limit = PageSize, offset = _offset, media_type = MediaType });
        DisplayPhotos(result.GetProperty("photos"), append: !reset);
        PageTitle.Text = MediaType == "video" ? "Videos / 视频" : MediaType == "photo" ? "Photos / 照片" : "All media / 全部";
        StatusText.Text = _photos.Count == 0 ? "Add a local folder to start scanning."
            : $"已显示 {_photos.Count:N0} 项。点击打开原文件；实况照片的视频部分随照片一起管理。";
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
        int named = result.TryGetProperty("filename_matches", out var n) ? n.GetInt32() : 0;
        if (mode != "semantic")
            StatusText.Text = $"Filename search only · {_photos.Count} results · Install AI for image-content search";
        else if (named > 0 && named == _photos.Count)
            StatusText.Text = $"文件名匹配 · {named} 个结果";
        else
            StatusText.Text = $"AI semantic search · {_photos.Count} results for \"{query}\"" +
                (named > 0 ? $" · 前 {named} 个为文件名匹配" : "");
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
        SelectButton.IsEnabled = !busy;
        SelectAllButton.IsEnabled = !busy;
        RecycleButton.IsEnabled = !busy && _selecting && PhotoGrid.SelectedItems.Count > 0;
    }

    private void HandleProgress(JsonElement payload)
    {
        var stage = Str(payload, "stage");
        if (stage == "indexing")
        {
            int count = payload.TryGetProperty("seen", out var seen) ? seen.GetInt32() : 0;
            int ai = payload.TryGetProperty("embedded", out var embeds) ? embeds.GetInt32() : 0;
            StatusText.Text = $"正在扫描 {count:N0} 个文件 · {ai:N0} AI indexed";
        }
        else if (stage == "fingerprinting")
        {
            int done = payload.TryGetProperty("done", out var d) ? d.GetInt32() : 0;
            int total = payload.TryGetProperty("total", out var t) ? t.GetInt32() : 0;
            StatusText.Text = $"正在比对重复文件 {done:N0}/{total:N0}（首次需读取全部视频，较慢）· 原文件不会被删除";
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
                $"{Str(p, "month")}: {p.GetProperty("count").GetInt32():N0} 项");
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
            if (similar.GetArrayLength() > 0) lines.AddRange(new[] { "", "相似照片（前 20 组，请人工核对）：" });
            foreach (var group in similar.EnumerateArray().Take(20))
                lines.Add("- " + string.Join(" | ", group.EnumerateArray().Select(p => Str(p, "name"))));
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
            await ShowMessageAsync("Background iPhone receiver enabled",
                "Local: http://127.0.0.1:8787" + Environment.NewLine +
                "From iPhone away from home: run this on Windows (Tailscale installed):" +
                Environment.NewLine + command + Environment.NewLine + Environment.NewLine +
                "Open the private HTTPS URL shown by Tailscale Serve on your iPhone." +
                Environment.NewLine + Environment.NewLine +
                "Pairing token (keep private): " + token + Environment.NewLine + Environment.NewLine +
                "The receiver stays active when this window closes. Reopen Manage sync and choose Stop receiver to stop it. It does not auto-start after sign-out or reboot." + Environment.NewLine + Environment.NewLine +
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
            if (!planned.GetProperty("complete").GetBoolean())
            {
                await ShowMessageAsync("Backup needs attention", "Some originals are stale, unavailable or unsafe. Scan/reconcile the library and reconnect source drives before backing up. Details: " + planned.GetProperty("issues").GetRawText());
                return;
            }
            var files = planned.GetProperty("files").GetInt32();
            var bytes = planned.GetProperty("bytes_planned").GetInt64();
            var dialog = new ContentDialog
            {
                Title = "Back up original photos?",
                Content = $"Destination: {folder.Path}\n{files:N0} original resource files\nUp to {bytes / (1024.0 * 1024 * 1024):F2} GB planned.\nOnly copies. No deletion and no folder mirroring.",
                PrimaryButtonText = "Start backup",
                CloseButtonText = "Cancel",
                XamlRoot = Root.XamlRoot
            };
            if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
            var done = await CallAsync("backup_local", new { target = folder.Path, apply = true });
            await ShowMessageAsync(done.GetProperty("complete").GetBoolean() ? "Backup verified" : "Backup incomplete",
                $"Copied: {done.GetProperty("copied").GetInt32()}\nUnchanged: {done.GetProperty("unchanged").GetInt32()}\nIssues: {done.GetProperty("issues").GetRawText()}");
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
    private async void MediaFilter_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_started || _engine is null) return;
        try { SearchBox.Text = ""; await BrowseAsync(true); }
        catch (Exception ex) { ShowError("Cannot filter library", ex); }
    }
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
    private void SelectButton_Click(object sender, RoutedEventArgs e)
    {
        if (_selecting)
        {
            PhotoGrid.SelectedItems.Clear();
            _selecting = false;
            PhotoGrid.SelectionMode = ListViewSelectionMode.None;
            PhotoGrid.IsItemClickEnabled = true;
        }
        else
        {
            PhotoGrid.IsItemClickEnabled = false;
            _selecting = true;
            PhotoGrid.SelectionMode = ListViewSelectionMode.Multiple;
        }
        SelectButton.Content = _selecting ? "完成选择" : "选择照片";
        SelectAllButton.Visibility = RecycleButton.Visibility = _selecting ? Visibility.Visible : Visibility.Collapsed;
    }

    private void SelectAllButton_Click(object sender, RoutedEventArgs e)
    {
        if (_selecting) PhotoGrid.SelectAll();
    }

    private void PhotoGrid_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (RecycleButton is null) return;
        var count = _selecting ? PhotoGrid.SelectedItems.Count : 0;
        RecycleButton.Content = $"移到回收站 ({count})";
        RecycleButton.IsEnabled = !_working && count > 0;
    }

    private async void RecycleButton_Click(object sender, RoutedEventArgs e)
    {
        if (_working) return;
        var selected = PhotoGrid.SelectedItems.OfType<PhotoCard>().ToArray();
        if (selected.Length == 0) return;
        SetBusy(true);
        try
        {
            var paths = selected.Select(p => p.Path).ToArray();
            var plan = await CallAsync("recycle_plan", new { paths });
            var files = plan.GetProperty("files").EnumerateArray().Select(r => new {
                Path = Str(r, "path"), Size = r.GetProperty("size").GetInt64(),
                MtimeNs = r.GetProperty("mtime_ns").GetInt64()
            }).ToArray();
            var candidates = plan.GetProperty("candidate_companions").EnumerateArray().Select(r => r.GetString() ?? "").ToArray();
            int confirmed = plan.TryGetProperty("confirmed_companions", out var c) ? c.GetArrayLength() : 0;
            var list = string.Join(Environment.NewLine, files.Select(f => f.Path));
            string pairing = (confirmed > 0 ? $"包含 {confirmed} 个实况照片的视频部分（已用 Apple 实况元数据确认）。" : "") +
                (candidates.Length > confirmed
                    ? $"另有 {candidates.Length - confirmed} 个同名配套文件仅按文件夹和文件名推测，并非可靠识别；若有无关视频请取消。"
                    : "");
            var confirm = new ContentDialog
            {
                Title = $"核对 {files.Length} 个将移到回收站的文件",
                Content = new ScrollViewer { MaxHeight = 350, Content = new TextBlock { TextWrapping = TextWrapping.Wrap,
                    Text = $"{pairing}请逐项核对。\n\n{list}" } },
                PrimaryButtonText = "文件正确，继续", CloseButtonText = "取消", DefaultButton = ContentDialogButton.Close, XamlRoot = Root.XamlRoot
            };
            if (await confirm.ShowAsync() != ContentDialogResult.Primary) return;
            var final = new ContentDialog
            {
                Title = "再次确认移动原文件",
                Content = $"确定将列出的 {files.Length} 个文件交给 Windows 移到回收站？如果 Windows 提示永久删除，请取消。部分操作失败时可能只移动部分文件。",
                PrimaryButtonText = "确认移到回收站", CloseButtonText = "取消", DefaultButton = ContentDialogButton.Close, XamlRoot = Root.XamlRoot
            };
            if (await final.ShowAsync() != ContentDialogResult.Primary) return;
            // Revalidate identity and protected sync references after the confirmation dialogs.
            var fresh = await CallAsync("recycle_plan", new { paths });
            if (fresh.GetProperty("files").GetRawText() != plan.GetProperty("files").GetRawText())
                throw new IOException("文件或配套候选已变化，请重新选择。");
            int moved = 0;
            foreach (var file in files)
            {
                try
                {
                    var root = Path.GetPathRoot(file.Path);
                    if (string.IsNullOrEmpty(root) || new DriveInfo(root).DriveType != DriveType.Fixed)
                        throw new IOException("仅支持本地固定磁盘上的文件");
                    var info = new FileInfo(file.Path);
                    long ns = (info.LastWriteTimeUtc.Ticks - DateTime.UnixEpoch.Ticks) * 100;
                    if (!info.Exists || info.Length != file.Size || ns != file.MtimeNs)
                        throw new IOException("文件已变化，请重新扫描");
                    Microsoft.VisualBasic.FileIO.FileSystem.DeleteFile(file.Path,
                        Microsoft.VisualBasic.FileIO.UIOption.AllDialogs,
                        Microsoft.VisualBasic.FileIO.RecycleOption.SendToRecycleBin,
                        Microsoft.VisualBasic.FileIO.UICancelOption.ThrowException);
                    foreach (var card in _photos.Where(p => string.Equals(p.Path, file.Path, StringComparison.OrdinalIgnoreCase)).ToArray())
                        _photos.Remove(card);
                    moved++;
                }
                catch (Exception ex)
                {
                    await ShowMessageAsync("操作中断", $"已移动 {moved} 个文件；其余未处理。\n{file.Path}\n{ex.Message}\n可在回收站恢复已移动的文件。");
                    break;
                }
            }
            StatusText.Text = $"已移到回收站: {moved}/{files.Length}。请扫描以更新索引。";
        }
        catch (Exception ex) { ShowError("无法准备回收站操作", ex); }
        finally { SetBusy(false); }
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
