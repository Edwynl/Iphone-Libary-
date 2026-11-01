using System.Collections.ObjectModel;
using System.Linq;
using System.Diagnostics;
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Windows.Storage.Pickers;
using Windows.System;
using WindowsAIPhotos.Models;
using WindowsAIPhotos.Services;

namespace WindowsAIPhotos;

public sealed partial class MainWindow : Window
{
    private const int PageSize = 120;
    private readonly ObservableCollection<PhotoCard> _photos = new();
    private readonly ObservableCollection<DuplicateGroup> _duplicateGroups = new();
    private readonly ObservableCollection<string> _folders = new();
    private PythonEngineBridge? _engine;
    private bool _started;
    private bool _working;
    private bool _isSearch;
    private int _offset;
    private bool _hasMore;
    private bool _loadingMore;
    private int _browseVersion;
    private ScrollViewer? _gridScroll;
    private IPhoneImporter? _iphone;
    private string _importDestination = "";
    private bool _loadingImportSettings;
    private bool _importing;
    private bool _selecting;
    // In MediaFilter order.
    private static readonly string[] MediaTypes = { "all", "photo", "video", "screenshot" };
    private static readonly string[] MediaTitles = { "All media / 全部", "Photos / 照片", "Videos / 视频", "Screenshots / 截图" };
    private int MediaIndex => Math.Max(0, MediaFilter.SelectedIndex);
    private string MediaType => MediaTypes[MediaIndex];
    // In SortOrder order; by capture date, else file time.
    private static readonly string[] SortOrders = { "newest", "oldest" };
    private string Order => SortOrders[Math.Max(0, SortOrder.SelectedIndex)];

    public MainWindow()
    {
        InitializeComponent();
        // Content runs under the caption buttons so the acrylic backdrop reaches the top edge.
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        InitializeBoxSelection();
        PhotoGrid.ItemsSource = _photos;
        DuplicateGroupList.ItemsSource = _duplicateGroups;
        PhotoGrid.Loaded += (_, _) => _gridScroll ??= FindChild<ScrollViewer>(PhotoGrid);
        FolderList.ItemsSource = _folders;
        Closed += (_, _) => { _iphone?.Dispose(); _engine?.Dispose(); };
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
            await LoadImportSettingsAsync();
            _iphone = new IPhoneImporter();
            _iphone.DeviceArrived += () => DispatcherQueue.TryEnqueue(async () =>
            {
                if (AutoImportToggle.IsOn) await ImportFromIPhoneAsync(auto: true);
            });
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
        int screenshots = data.TryGetProperty("screenshots", out var s) ? s.GetInt32() : 0;
        PhotoCount.Text = $"{count:N0} 张照片（{live:N0} 实况，{screenshots:N0} 截图） · {videos:N0} 个视频";
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

    /// <summary>A card for an engine photo row (browse, search or duplicate groups).</summary>
    private static PhotoCard? CardFrom(JsonElement row)
    {
        var path = Str(row, "path");
        if (path.Length == 0) return null;
        var date = row.TryGetProperty("mtime_ns", out var element) && element.TryGetInt64(out var ns) ? ns : 0;
        return PhotoCard.Create(path, Str(row, "name"), Str(row, "thumb_path"), date, Str(row, "taken_at"),
            Str(row, "media_kind"), Str(row, "companion"));
    }

    private void DisplayPhotos(JsonElement json, bool append)
    {
        if (!append)
        {
            _photos.Clear();
            _anchorIndex = null;
        }
        foreach (var row in json.EnumerateArray())
        {
            if (CardFrom(row) is { } card) _photos.Add(card);
        }
        EmptyState.Visibility = _photos.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        EmptyState.Text = _folders.Count == 0 ? "Choose Add photo folder to begin."
            : _isSearch ? "No matching photos found." : "No photos indexed yet. Choose Scan.";
        _hasMore = !_isSearch && json.GetArrayLength() == PageSize;
    }

    private async Task BrowseAsync(bool reset)
    {
        if (reset) _offset = 0;
        // A newer browse or search supersedes this page; never append it to their results.
        int version = reset ? ++_browseVersion : _browseVersion;
        var result = await CallAsync("browse", new { limit = PageSize, offset = _offset, media_type = MediaType, order = Order });
        if (version != _browseVersion) return;
        _isSearch = false;
        DisplayPhotos(result.GetProperty("photos"), append: !reset);
        QueueNextPage();
        PageTitle.Text = MediaTitles[MediaIndex];
        StatusText.Text = _photos.Count == 0 ? "Add a local folder to start scanning."
            : $"已显示 {_photos.Count:N0} 项。点击打开原文件；实况照片的视频部分随照片一起管理。";
    }

    private async Task SearchAsync()
    {
        string query = SearchBox.Text.Trim();
        ShowDuplicateView(false);
        if (query.Length == 0) { await BrowseAsync(true); return; }
        _browseVersion++;
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
        IPhoneImportButton.IsEnabled = !busy;
        BackupButton.IsEnabled = !busy;
        CloudButton.IsEnabled = !busy;
        SelectButton.IsEnabled = !busy;
        SelectAllButton.IsEnabled = !busy;
        RecycleButton.IsEnabled = !busy && _selecting && PhotoGrid.SelectedItems.Count > 0;
        UpdateDuplicateSelection();
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
        else if (stage == "importing")
        {
            int done = payload.TryGetProperty("done", out var d) ? d.GetInt32() : 0;
            int total = payload.TryGetProperty("total", out var t) ? t.GetInt32() : 0;
            StatusText.Text = $"正在去重并归档 iPhone 照片 {done:N0}/{total:N0}";
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

    private const string IPhoneHint = "请用数据线连接 iPhone 并解锁，在手机上点“信任此电脑”。" +
        "如果仍然找不到，请安装 Apple 设备驱动（Microsoft Store 中的“Apple 设备”App 或 iTunes）。";

    private async Task LoadImportSettingsAsync()
    {
        var settings = await CallAsync("device_import_settings");
        _importDestination = Str(settings, "destination");
        ShowImportDestination();
        _loadingImportSettings = true;
        AutoImportToggle.IsOn = settings.GetProperty("auto").GetBoolean();
        _loadingImportSettings = false;
    }

    private async Task<bool> EnsureImportDestinationAsync()
    {
        if (_importDestination.Length > 0 && Directory.Exists(_importDestination)) return true;
        return await PickImportDestinationAsync();
    }

    private async Task<bool> PickImportDestinationAsync()
    {
        await ShowMessageAsync("选择 iPhone 照片的保存位置",
            "iPhone 的照片和视频会按月份（例如 2026-10）复制到这个文件夹，并自动加入图库。iPhone 上的内容不会被删除或修改。" +
            "\n\n不要选已在图库中的文件夹的上级文件夹（例如已添加 F:\\iphone\\2026.09.19 时不要选 F:\\iphone）。");
        var picker = new FolderPicker();
        picker.FileTypeFilter.Add("*");
        WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
        var folder = await picker.PickSingleFolderAsync();
        if (folder is null) return false;
        await CallAsync("device_import_settings", new { destination = folder.Path });
        _importDestination = folder.Path;
        ShowImportDestination();
        return true;
    }

    private void ShowImportDestination() =>
        ImportDestinationText.Text = _importDestination.Length == 0
            ? "保存位置：未设置（点此选择）" : $"保存到：{_importDestination}（点此更改）";

    private async void ImportDestinationButton_Click(object sender, RoutedEventArgs e)
    {
        if (_working) return;
        try { await PickImportDestinationAsync(); }
        catch (Exception ex) { ShowError("Cannot change iPhone import folder", ex); }
    }

    private async void IPhoneImportButton_Click(object sender, RoutedEventArgs e) =>
        await ImportFromIPhoneAsync(auto: false);

    private async void AutoImportToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_loadingImportSettings) return;
        try
        {
            if (AutoImportToggle.IsOn && !await EnsureImportDestinationAsync())
            {
                _loadingImportSettings = true;
                AutoImportToggle.IsOn = false;
                _loadingImportSettings = false;
                return;
            }
            await CallAsync("device_import_settings", new { auto = AutoImportToggle.IsOn });
            if (AutoImportToggle.IsOn) await ImportFromIPhoneAsync(auto: true);  // It may already be plugged in.
        }
        catch (Exception ex) { ShowError("Cannot change iPhone import setting", ex); }
    }

    private async Task ImportFromIPhoneAsync(bool auto)
    {
        // Device arrival fires once per interface; only the first import proceeds.
        if (_working || _importing) return;
        _importing = true;
        try
        {
            var source = await IPhoneImporter.FindIPhoneAsync();
            if (source is null)
            {
                if (!auto) await ShowMessageAsync("没有找到 iPhone", IPhoneHint);
                return;
            }
            if (!await EnsureImportDestinationAsync()) return;
            IPhoneImportOutcome outcome;
            SetBusy(true);
            try
            {
                outcome = await IPhoneImporter.ImportAsync(source, _importDestination, CallAsync,
                    text => DispatcherQueue.TryEnqueue(() => StatusText.Text = text));
            }
            finally { SetBusy(false); }

            if (outcome.State is "locked" or "empty")
            {
                // Tapping "Trust" re-announces the device, so auto mode retries by itself.
                StatusText.Text = "已检测到 iPhone，但读不到相册：请解锁手机并点“信任此电脑”" +
                    (auto ? "，之后会自动开始导入。" : "，然后再点“从 iPhone 导入”。");
                if (!auto) await ShowMessageAsync("iPhone 相册暂时读不到", IPhoneHint);
                return;
            }
            if (outcome.Copied > 0)
            {
                await RefreshFoldersAsync();
                await ScanAsync();
            }
            if (outcome.State == "nothing-new")
                StatusText.Text = $"iPhone 已是最新：{outcome.Found:N0} 项都已核实导入到这台电脑。";
            else
                StatusText.Text = (outcome.State == "interrupted" ? "iPhone 导入中断（已复制的部分已保存，重新连接后会继续）：" : "iPhone 导入完成：") +
                    $"新增 {outcome.Copied:N0} 个文件，跳过已核实导入的 {outcome.AlreadyHave:N0} 项，" +
                    $"去掉与图库内容完全相同的 {outcome.Duplicates:N0} 个。" +
                    (outcome.Reverify > 0 ? $"其中 {outcome.Reverify:N0} 项来自旧版导入记录（没有校验值），这次已重新复制并逐字节核对。" : "") +
                    (outcome.Incomplete > 0 ? $"{outcome.Incomplete:N0} 个文件复制不完整已丢弃，下次导入会重新复制。" : "") +
                    $"保存在 {_importDestination}";
            if (outcome.State == "interrupted")
                await ShowMessageAsync("iPhone 导入中断", "已复制的照片已保存。请保持数据线连接并解锁 iPhone，再点“从 iPhone 导入”继续。\n\n" + outcome.Error);
        }
        catch (Exception ex) { ShowError("iPhone import failed", ex); }
        finally { _importing = false; }
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
            _duplicateGroups.Clear();
            foreach (var (key, exact) in new[] { ("exact_groups", true), ("similar_groups", false) })
            {
                foreach (var group in data.GetProperty(key).EnumerateArray())
                {
                    var cards = group.EnumerateArray().Select(CardFrom).OfType<PhotoCard>();
                    _duplicateGroups.Add(new DuplicateGroup { Exact = exact, Photos = new(cards) });
                }
            }
            StatusText.Text = "Duplicate analysis finished. Photo originals remain untouched.";
            if (_duplicateGroups.Count == 0)
            {
                await ShowMessageAsync("Duplicate photo review", "没有找到重复或相似的照片。");
                return;
            }
            ShowDuplicateView(true);
            UpdateDuplicateSelection();
        }
        catch (Exception ex) { ShowError("Duplicate scan failed", ex); }
        finally { SetBusy(false); }
    }

    private void ShowDuplicateView(bool show)
    {
        DuplicateView.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        LibraryView.Visibility = show ? Visibility.Collapsed : Visibility.Visible;
        PageTitle.Text = show ? "Duplicates / 重复照片" : MediaTitles[MediaIndex];
        if (!show) _duplicateGroups.Clear();
    }

    private void UpdateDuplicateSelection()
    {
        int exact = _duplicateGroups.Count(g => g.Exact);
        int selected = _duplicateGroups.Sum(g => g.Photos.Count(p => p.IsSelected));
        DuplicateSummary.Text = $"完全相同 {exact} 组 · 相似 {_duplicateGroups.Count - exact} 组。" +
            "点击照片选中，双击放大比较（← → 切换同组照片）；不会自动删除任何文件。";
        DuplicateRecycleButton.Content = $"移到回收站 ({selected})";
        DuplicateRecycleButton.IsEnabled = !_working && selected > 0;
        SelectExtrasButton.IsEnabled = !_working && exact > 0;
    }

    private void DuplicateCard_Click(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is not PhotoCard card) return;
        card.IsSelected = !card.IsSelected;
        UpdateDuplicateSelection();
    }

    // Two clicks first toggle the mark twice, leaving it as it was. The viewer steps through the group.
    private void DuplicateCard_DoubleTapped(object sender, DoubleTappedRoutedEventArgs e)
    {
        if ((e.OriginalSource as FrameworkElement)?.DataContext is not PhotoCard card) return;
        if (_duplicateGroups.FirstOrDefault(g => g.Photos.Contains(card)) is { } group) OpenViewer(group.Photos, card);
    }

    // Byte-identical copies: keep the first of each group. Similar photos are never picked for the user.
    private void SelectExtrasButton_Click(object sender, RoutedEventArgs e)
    {
        foreach (var group in _duplicateGroups.Where(g => g.Exact))
        {
            for (int i = 0; i < group.Photos.Count; i++) group.Photos[i].IsSelected = i > 0;
        }
        UpdateDuplicateSelection();
    }

    private void ClearDuplicateSelection_Click(object sender, RoutedEventArgs e)
    {
        foreach (var card in _duplicateGroups.SelectMany(g => g.Photos)) card.IsSelected = false;
        UpdateDuplicateSelection();
    }

    private async void DuplicateRecycleButton_Click(object sender, RoutedEventArgs e)
    {
        if (_working) return;
        var paths = _duplicateGroups.SelectMany(g => g.Photos).Where(p => p.IsSelected).Select(p => p.Path).ToArray();
        if (paths.Length == 0) return;
        var moved = await RecyclePathsAsync(paths);
        foreach (var group in _duplicateGroups.ToArray())
        {
            foreach (var card in group.Photos.Where(p => moved.Contains(p.Path)).ToArray()) group.Photos.Remove(card);
            if (group.Photos.Count < 2) _duplicateGroups.Remove(group);
        }
        UpdateDuplicateSelection();
    }

    private void CloseDuplicatesButton_Click(object sender, RoutedEventArgs e) => ShowDuplicateView(false);

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
        try { ShowDuplicateView(false); SearchBox.Text = ""; await BrowseAsync(true); }
        catch (Exception ex) { ShowError("Cannot filter library", ex); }
    }
    private async void LibraryButton_Click(object sender, RoutedEventArgs e)
    {
        try { ShowDuplicateView(false); SearchBox.Text = ""; await BrowseAsync(true); }
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
    private async Task LoadNextPageAsync()
    {
        if (_loadingMore || !_hasMore || _isSearch) return;
        _loadingMore = true;
        try { _offset = _photos.Count; await BrowseAsync(reset: false); }
        catch (Exception ex) { ShowError("Cannot load more photos", ex); }
        finally { _loadingMore = false; }
    }

    // Preload the whole library page by page at low priority (cards hold only a file path),
    // so scrolling never stops to fetch and lay out a page. Each page queues the next.
    private void QueueNextPage() =>
        DispatcherQueue.TryEnqueue(Microsoft.UI.Dispatching.DispatcherQueuePriority.Low,
            () => _ = LoadNextPageAsync());

    private void ThumbSize_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (PhotoGrid?.ItemsPanelRoot is not ItemsWrapGrid panel) return;
        panel.ItemWidth = e.NewValue;
        panel.ItemHeight = Math.Round(e.NewValue * 0.77) + 53;  // Image area plus two caption lines.
    }

    private static T? FindChild<T>(DependencyObject parent) where T : DependencyObject
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        {
            var child = VisualTreeHelper.GetChild(parent, i);
            if (child is T match) return match;
            if (FindChild<T>(child) is { } nested) return nested;
        }
        return null;
    }
    private void SelectButton_Click(object sender, RoutedEventArgs e) => SetSelecting(!_selecting);

    private void SetSelecting(bool selecting)
    {
        if (!selecting)
        {
            PhotoGrid.SelectedItems.Clear();
            _selecting = false;
            _anchorIndex = null;
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
        var selected = PhotoGrid.SelectedItems.OfType<PhotoCard>().ToHashSet();
        foreach (var card in _photos) card.IsSelected = selected.Contains(card);
        var count = _selecting ? PhotoGrid.SelectedItems.Count : 0;
        RecycleButton.Content = $"移到回收站 ({count})";
        RecycleButton.IsEnabled = !_working && count > 0;
    }

    private async void RecycleButton_Click(object sender, RoutedEventArgs e)
    {
        if (_working) return;
        var paths = PhotoGrid.SelectedItems.OfType<PhotoCard>().Select(p => p.Path).ToArray();
        if (paths.Length > 0) await RecyclePathsAsync(paths);
    }

    /// <summary>Reviews, confirms twice and sends files (and their Live Photo companions) to the
    /// Recycle Bin. Returns the paths actually moved; their library cards are removed.</summary>
    private async Task<HashSet<string>> RecyclePathsAsync(string[] paths)
    {
        var moved = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        SetBusy(true);
        try
        {
            var plan = await CallAsync("recycle_plan", new { paths });
            var files = plan.GetProperty("files").EnumerateArray().Select(r => new {
                Path = Str(r, "path"), Size = r.GetProperty("size").GetInt64(),
                MtimeNs = r.GetProperty("mtime_ns").GetInt64()
            }).ToArray();
            int confirmed = plan.GetProperty("confirmed_companions").GetArrayLength();
            int suggested = plan.GetProperty("suggested_companions").GetArrayLength();
            var list = string.Join(Environment.NewLine, files.Select(f => f.Path));
            string pairing = (confirmed > 0 ? $"包含 {confirmed} 个实况照片的视频部分（照片与视频的 Apple 实况标识一致）。" : "") +
                (suggested > 0 ? $"另有 {suggested} 个同名文件无法确认是同一张实况照片，不会移动；如需删除请单独选择。" : "");
            var confirm = new ContentDialog
            {
                Title = $"核对 {files.Length} 个将移到回收站的文件",
                Content = new ScrollViewer { MaxHeight = 350, Content = new TextBlock { TextWrapping = TextWrapping.Wrap,
                    Text = $"{pairing}请逐项核对。\n\n{list}" } },
                PrimaryButtonText = "文件正确，继续", CloseButtonText = "取消", DefaultButton = ContentDialogButton.Close, XamlRoot = Root.XamlRoot
            };
            if (await confirm.ShowAsync() != ContentDialogResult.Primary) return moved;
            var final = new ContentDialog
            {
                Title = "再次确认移动原文件",
                Content = $"确定将列出的 {files.Length} 个文件交给 Windows 移到回收站？如果 Windows 提示永久删除，请取消。部分操作失败时可能只移动部分文件。",
                PrimaryButtonText = "确认移到回收站", CloseButtonText = "取消", DefaultButton = ContentDialogButton.Close, XamlRoot = Root.XamlRoot
            };
            if (await final.ShowAsync() != ContentDialogResult.Primary) return moved;
            // Revalidate identity and protected sync references after the confirmation dialogs.
            var fresh = await CallAsync("recycle_plan", new { paths });
            if (fresh.GetProperty("files").GetRawText() != plan.GetProperty("files").GetRawText())
                throw new IOException("文件或配套候选已变化，请重新选择。");
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
                    moved.Add(file.Path);
                }
                catch (Exception ex)
                {
                    await ShowMessageAsync("操作中断", $"已移动 {moved.Count} 个文件；其余未处理。\n{file.Path}\n{ex.Message}\n可在回收站恢复已移动的文件。");
                    break;
                }
            }
            StatusText.Text = $"已移到回收站: {moved.Count}/{files.Length}。请扫描以更新索引。";
        }
        catch (Exception ex) { ShowError("无法准备回收站操作", ex); }
        finally { SetBusy(false); }
        return moved;
    }

    private void PhotoGrid_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is PhotoCard photo) OpenViewer(_photos, photo);
    }

    private void OpenOriginal(PhotoCard photo)
    {
        try { Process.Start(new ProcessStartInfo(photo.Path) { UseShellExecute = true }); }
        catch (Exception ex) { ShowError("Cannot open original photo", ex); }
    }
    private void ShowError(string prefix, Exception ex)
    {
        StatusText.Text = $"{prefix}: {ex.Message}";
        Debug.WriteLine(ex);
    }
}
