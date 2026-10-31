using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text.Json;
using Windows.Devices.Enumeration;
using Windows.Media.Import;

namespace WindowsAIPhotos.Services;

public sealed record IPhoneImportOutcome(string State, int Found = 0, int AlreadyHave = 0, int Copied = 0,
    int Duplicates = 0, string? Error = null);

/// <summary>
/// Copies new camera-roll files from a USB-connected iPhone into the library. The phone
/// is read through the Windows Shell (Explorer's path to "Internal Storage\DCIM", or to
/// Internal Storage itself where the camera-roll folders sit directly); nothing
/// on it is changed. The engine decides which files are new and files the copies.
/// </summary>
/// <remarks>
/// Windows.Media.Import finds the iPhone but its copy step fails for unpackaged apps,
/// so it is used only to recognise the device.
/// </remarks>
public sealed class IPhoneImporter : IDisposable
{
    // Portable devices (phones, cameras); an iPhone appears after "Trust This Computer".
    private const string WpdInterfaces =
        "System.Devices.InterfaceClassGuid:=\"{6AC27878-A6FA-4155-BA85-F98F491D4F33}\" AND " +
        "System.Devices.InterfaceEnabled:=System.StructuredQueryType.Boolean#True";
    private const string StagingDir = ".wap-importing";  // Matches engine/ai_photos/device_import.py.
    private const int ThisPc = 17;  // ShellSpecialFolderConstants.ssfDRIVES
    private readonly DeviceWatcher _watcher;
    private bool _enumerated;

    /// <summary>Raised once after start-up enumeration and whenever a portable device connects.</summary>
    public event Action? DeviceArrived;

    public IPhoneImporter()
    {
        _watcher = DeviceInformation.CreateWatcher(WpdInterfaces);
        _watcher.Added += (_, _) => { if (_enumerated) DeviceArrived?.Invoke(); };
        // A watcher stops delivering Added unless Updated and Removed are also handled.
        _watcher.Updated += (_, _) => { };
        _watcher.Removed += (_, _) => { };
        _watcher.EnumerationCompleted += (_, _) => { _enumerated = true; DeviceArrived?.Invoke(); };
        _watcher.Start();
    }

    public void Dispose()
    {
        if (_watcher.Status is DeviceWatcherStatus.Started or DeviceWatcherStatus.EnumerationCompleted)
            _watcher.Stop();
    }

    public static async Task<PhotoImportSource?> FindIPhoneAsync()
    {
        foreach (var source in await PhotoImportManager.FindAllSourcesAsync())
        {
            if ((source.Manufacturer ?? "").Contains("Apple", StringComparison.OrdinalIgnoreCase) ||
                (source.DisplayName ?? "").Contains("iPhone", StringComparison.OrdinalIgnoreCase))
                return source;
        }
        return null;
    }

    public static Task<IPhoneImportOutcome> ImportAsync(PhotoImportSource source, string destination,
        Func<string, object?, Task<JsonElement>> call, Action<string> status)
    {
        if (source.IsLocked == true) return Task.FromResult(new IPhoneImportOutcome("locked"));
        string deviceId = string.IsNullOrEmpty(source.SerialNumber) ? source.Id
            : $"{source.Manufacturer}|{source.Model}|{source.SerialNumber}";
        return ImportFolderAsync(deviceId, () => OpenDcim(source.DisplayName), destination, call, status);
    }

    /// <summary>Imports media under a Shell folder: the iPhone's DCIM, or any folder in tests.</summary>
    public static async Task<IPhoneImportOutcome> ImportFolderAsync(string deviceId, Func<dynamic?> openRoot,
        string destination, Func<string, object?, Task<JsonElement>> call, Action<string> status)
    {
        status("正在读取 iPhone 相册…");
        List<DeviceFile> files = await OnShellThread<List<DeviceFile>>(() =>
        {
            dynamic? root = openRoot();
            return root is null ? new List<DeviceFile>() : ListFiles(root);
        });
        // A locked or untrusted iPhone shows no DCIM content.
        if (files.Count == 0) return new("empty");

        var plan = await call("device_import_plan", new
        {
            device_id = deviceId,
            items = files.Select(f => new { key = f.ShellPath, name = f.Name, size = f.Size, date = f.Date })
        });
        var selected = plan.GetProperty("selected").EnumerateArray().Select(e => e.GetString()).ToHashSet();
        var wanted = files.Where(f => selected.Contains(f.ShellPath)).ToList();
        if (wanted.Count == 0) return new("nothing-new", files.Count, files.Count);

        var staging = Path.Combine(destination, StagingDir, DateTime.Now.ToString("yyyyMMdd-HHmmss"));
        Directory.CreateDirectory(staging);
        Exception? interruption = null;
        var copied = await OnShellThread(() =>
        {
            var done = new List<object>();
            foreach (var file in wanted)
            {
                status($"正在从 iPhone 复制 {done.Count + 1:N0}/{wanted.Count:N0}：{file.Name}");
                // Subfolders keep IMG_0001 from 100APPLE and 101APPLE apart.
                var relative = Path.Combine(file.Folder, file.Name);
                try
                {
                    var target = Path.Combine(staging, relative);
                    CopyStream(file.IdList, target);
                    // Keep the phone's time, as Explorer does: undated images sort by it.
                    File.SetLastWriteTimeUtc(target, DateTimeOffset.Parse(file.Date).UtcDateTime);
                }
                catch (Exception ex) when (ex is IOException or COMException or UnauthorizedAccessException)
                {
                    interruption = ex;  // E.g. unplugged: still keep everything copied so far.
                    break;
                }
                done.Add(new { name = file.Name, size = file.Size, date = file.Date, files = new[] { relative } });
            }
            return done;
        });
        status("正在去重并归档…");
        // The engine files what arrived and removes the staging folders it emptied.
        var result = await call("device_import_commit", new { device_id = deviceId, destination, staging, items = copied });
        return new(interruption is null ? "done" : "interrupted", files.Count, files.Count - wanted.Count,
            result.GetProperty("imported").GetInt32(), result.GetProperty("duplicates").GetInt32(), interruption?.Message);
    }

    // IdList: the item's absolute PIDL bytes. A phone item's ShellPath does not parse back into an
    // item, and Shell objects cannot cross threads, but a PIDL is plain data that opens it anywhere.
    private sealed record DeviceFile(string ShellPath, byte[] IdList, string Folder, string Name, long Size, string Date);

    private static dynamic Shell() => Activator.CreateInstance(Type.GetTypeFromProgID("Shell.Application")!)!;

    /// <summary>This PC → the iPhone → Internal Storage → DCIM. Some iPhones list the camera-roll
    /// folders (202610_a, 100APPLE, ...) directly under Internal Storage; then that folder is the root.</summary>
    private static dynamic? OpenDcim(string displayName)
    {
        dynamic? device = null;
        foreach (dynamic item in Shell().NameSpace(ThisPc).Items())
        {
            string name = item.Name;
            if (name == displayName) { device = item; break; }
            if (device is null && name.Contains("iPhone", StringComparison.OrdinalIgnoreCase)) device = item;
        }
        if (device is null || !device.IsFolder) return null;
        dynamic? storageWithFolders = null;
        foreach (dynamic storage in device.GetFolder.Items())
        {
            if (!storage.IsFolder) continue;
            foreach (dynamic child in storage.GetFolder.Items())
            {
                if (!child.IsFolder) continue;
                if (string.Equals((string)child.Name, "DCIM", StringComparison.OrdinalIgnoreCase)) return child.GetFolder;
                storageWithFolders ??= storage.GetFolder;
            }
        }
        // A locked or untrusted iPhone shows an empty Internal Storage, so this is still null then.
        return storageWithFolders;
    }

    private static readonly HashSet<string> MediaExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".tif", ".tiff", ".dng",
        ".mov", ".mp4", ".m4v"
    };

    private static List<DeviceFile> ListFiles(dynamic root)
    {
        var files = new List<DeviceFile>();
        void Walk(dynamic folder, string relative, int depth)
        {
            foreach (dynamic item in folder.Items())
            {
                if (item.IsFolder)
                {
                    if (depth < 2) Walk(item.GetFolder, Path.Combine(relative, (string)item.Name), depth + 1);
                    continue;
                }
                // Item names may hide extensions (Explorer setting); System.FileName never does.
                string name = item.ExtendedProperty("System.FileName") as string ?? (string)item.Name;
                if (!MediaExtensions.Contains(Path.GetExtension(name))) continue;
                long size = Convert.ToInt64(item.ExtendedProperty("System.Size") ?? 0L);
                files.Add(new DeviceFile(item.Path, IdListOf(item), relative, name, size, ModifiedDate(item)));
            }
        }
        Walk(root, "", 0);
        return files;
    }

    /// <summary>Local ISO time the engine files the item under (destination/YYYY-MM).</summary>
    /// <remarks>An iPhone item's ModifyDate is always 1899-12-30; System.DateModified holds the
    /// real time, in UTC though its Kind is Unspecified.</remarks>
    private static string ModifiedDate(dynamic item)
    {
        object? utc = item.ExtendedProperty("System.DateModified");
        DateTimeOffset modified = utc is DateTime d
            ? new DateTimeOffset(DateTime.SpecifyKind(d, DateTimeKind.Utc)).ToLocalTime()
            : new DateTimeOffset((DateTime)item.ModifyDate);
        return modified.ToString("o");
    }

    /// <summary>Shell automation objects need an STA thread; keep the UI thread free meanwhile.</summary>
    private static Task<T> OnShellThread<T>(Func<T> work)
    {
        var done = new TaskCompletionSource<T>();
        var thread = new Thread(() =>
        {
            try { done.SetResult(work()); }
            catch (Exception ex) { done.SetException(ex); }
        }) { IsBackground = true, Name = "iPhone import" };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        return done.Task;
    }

    private static byte[] IdListOf(object folderItem)
    {
        SHGetIDListFromObject(folderItem, out var pidl);
        try
        {
            var bytes = new byte[ILGetSize(pidl)];
            Marshal.Copy(pidl, bytes, 0, bytes.Length);
            return bytes;
        }
        finally { Marshal.FreeCoTaskMem(pidl); }
    }

    private static void CopyStream(byte[] idList, string target)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        var bhidStream = new Guid("1CEBB3AB-7C10-499a-A417-92CA16C4CB83");
        var iidStream = new Guid("0000000c-0000-0000-C000-000000000046");
        var iidShellItem = typeof(IShellItem).GUID;
        IShellItem item;
        var pinned = GCHandle.Alloc(idList, GCHandleType.Pinned);
        try { SHCreateItemFromIDList(pinned.AddrOfPinnedObject(), ref iidShellItem, out item); }
        finally { pinned.Free(); }
        item.BindToHandler(IntPtr.Zero, ref bhidStream, ref iidStream, out var handler);
        var stream = (IStream)handler;
        var partial = target + ".part";
        var buffer = new byte[1 << 20];
        var read = Marshal.AllocHGlobal(sizeof(int));
        try
        {
            using (var output = new FileStream(partial, FileMode.CreateNew, FileAccess.Write))
            {
                while (true)
                {
                    stream.Read(buffer, buffer.Length, read);
                    int count = Marshal.ReadInt32(read);
                    if (count == 0) break;
                    output.Write(buffer, 0, count);
                }
            }
            File.Move(partial, target);  // Never overwrites.
        }
        catch
        {
            File.Delete(partial);  // Our own incomplete copy.
            throw;
        }
        finally
        {
            Marshal.FreeHGlobal(read);
            Marshal.ReleaseComObject(stream);
            Marshal.ReleaseComObject(item);
        }
    }

    [DllImport("shell32.dll", PreserveSig = false)]
    private static extern void SHGetIDListFromObject([MarshalAs(UnmanagedType.IUnknown)] object item, out IntPtr pidl);

    [DllImport("shell32.dll")]
    private static extern uint ILGetSize(IntPtr pidl);

    [DllImport("shell32.dll", PreserveSig = false)]
    private static extern void SHCreateItemFromIDList(IntPtr pidl, ref Guid riid,
        [MarshalAs(UnmanagedType.Interface)] out IShellItem item);

    [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItem
    {
        void BindToHandler(IntPtr bindContext, ref Guid handler, ref Guid riid,
            [MarshalAs(UnmanagedType.Interface)] out object result);
    }
}
