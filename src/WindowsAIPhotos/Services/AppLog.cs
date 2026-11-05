namespace WindowsAIPhotos.Services;

/// <summary>
/// Diagnostics at %LOCALAPPDATA%\WindowsAIPhotos\logs\app.log, next to the engine's logs.
/// Never throws: a failed log write must not take the app down with it.
/// </summary>
public static class AppLog
{
    private const long MaxBytes = 1024 * 1024;
    private static readonly object Gate = new();

    public static string FilePath { get; } = System.IO.Path.Combine(Home(), "logs", "app.log");

    // Mirrors app_home() in the engine's config.py.
    private static string Home()
    {
        var overridden = Environment.GetEnvironmentVariable("AI_PHOTOS_HOME");
        return !string.IsNullOrEmpty(overridden) ? System.IO.Path.GetFullPath(overridden)
            : System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WindowsAIPhotos");
    }

    public static void Write(string message)
    {
        try
        {
            lock (Gate)
            {
                Directory.CreateDirectory(System.IO.Path.GetDirectoryName(FilePath)!);
                var existing = new FileInfo(FilePath);
                if (existing.Exists && existing.Length > MaxBytes)
                    File.Move(FilePath, FilePath + ".1", overwrite: true);
                File.AppendAllText(FilePath, $"{DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss.fff zzz} {message}{Environment.NewLine}");
            }
        }
        catch (Exception) { }
    }

    public static void Error(string context, object? error) => Write($"ERROR {context}: {error}");
}
