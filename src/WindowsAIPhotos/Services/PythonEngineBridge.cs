using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json;

namespace WindowsAIPhotos.Services;

/// <summary>
/// A persistent local JSONL process. No HTTP listener, network API or cloud LLM.
/// AI operations are explicit and backed by files under %LOCALAPPDATA%.
/// </summary>
public sealed class PythonEngineBridge : IDisposable
{
    private readonly ConcurrentDictionary<int, TaskCompletionSource<JsonElement>> _pending = new();
    private readonly SemaphoreSlim _stdinLock = new(1, 1);
    private Process? _process;
    private int _nextId;
    private bool _disposed;
    private string _lastError = "";

    public event Action<JsonElement>? Progress;

    public Task StartAsync()
    {
        var engineDir = System.IO.Path.Combine(AppContext.BaseDirectory, "engine");
        if (!Directory.Exists(engineDir))
            throw new DirectoryNotFoundException($"AI engine files missing: {engineDir}");

        // Override for a virtualenv interpreter (e.g. .venv\Scripts\python.exe).
        var interpreter = Environment.GetEnvironmentVariable("AI_PHOTOS_PYTHON");
        var executable = string.IsNullOrWhiteSpace(interpreter) ? "python" : interpreter;
        var psi = new ProcessStartInfo(executable)
        {
            WorkingDirectory = engineDir,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        psi.ArgumentList.Add("-u");
        psi.ArgumentList.Add("-m");
        psi.ArgumentList.Add("ai_photos");
        psi.Environment["PYTHONIOENCODING"] = "utf-8";

        _process = Process.Start(psi) ?? throw new InvalidOperationException("Python engine could not start");
        _ = Task.Run(ReadStdoutAsync);
        _ = Task.Run(ReadStderrAsync);
        return Task.CompletedTask;
    }

    public async Task<JsonElement> RequestAsync(string action, object? parameters = null,
        CancellationToken cancellationToken = default)
    {
        if (_disposed || _process is null || _process.HasExited)
            throw new InvalidOperationException("Local photo engine is not running. " + _lastError);

        var id = Interlocked.Increment(ref _nextId);
        var source = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        if (!_pending.TryAdd(id, source)) throw new InvalidOperationException("Duplicate request ID");
        var message = JsonSerializer.Serialize(new { id, action, @params = parameters ?? new { } });
        try
        {
            await _stdinLock.WaitAsync(cancellationToken);
            try
            {
                await _process.StandardInput.WriteLineAsync(message);
                await _process.StandardInput.FlushAsync();
            }
            finally { _stdinLock.Release(); }
        }
        catch
        {
            _pending.TryRemove(id, out _);
            throw;
        }
        try
        {
            var timeout = action is "scan" or "install_model" or "backup_local" or "backup_s3" or "duplicates"
                ? TimeSpan.FromHours(12) : TimeSpan.FromMinutes(action == "search" ? 5 : 1);
            return await source.Task.WaitAsync(timeout, cancellationToken);
        }
        finally { _pending.TryRemove(id, out _); }
    }

    private async Task ReadStdoutAsync()
    {
        try
        {
            while (_process is { } proc)
            {
                string? line = await proc.StandardOutput.ReadLineAsync();
                if (line is null) break;
                try
                {
                    using var json = JsonDocument.Parse(line);
                    var root = json.RootElement;
                    if (root.TryGetProperty("event", out _))
                    {
                        if (root.TryGetProperty("data", out var progress)) Progress?.Invoke(progress.Clone());
                        continue;
                    }
                    if (!root.TryGetProperty("id", out var idElement) ||
                        !idElement.TryGetInt32(out var id) || !_pending.TryRemove(id, out var source))
                        continue;
                    try
                    {
                        if (root.GetProperty("ok").GetBoolean())
                            source.TrySetResult(root.GetProperty("data").Clone());
                        else
                            source.TrySetException(new InvalidOperationException(
                                root.TryGetProperty("error", out var err) ? err.GetString() : "Unknown engine error"));
                    }
                    catch (Exception ex) { source.TrySetException(new IOException("Invalid engine response", ex)); }
                }
                catch (JsonException ex)
                {
                    _lastError = $"Engine emitted malformed JSON: {ex.Message}";
                }
            }
        }
        catch (Exception ex) { _lastError = ex.Message; }
        finally
        {
            var error = new IOException("Local photo engine stopped. " + _lastError);
            foreach (var item in _pending)
                if (_pending.TryRemove(item.Key, out var source)) source.TrySetException(error);
        }
    }

    private async Task ReadStderrAsync()
    {
        try
        {
            while (_process is { } proc)
            {
                var line = await proc.StandardError.ReadLineAsync();
                if (line is null) break;
                _lastError = line.Length > 500 ? line[..500] : line;
            }
        }
        catch (Exception) { }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        try
        {
            // The explicitly enabled receiver is a detached process with its own
            // stop control. Closing this window only stops the stdio worker.
            if (_process is { HasExited: false }) _process.Kill(entireProcessTree: false);
        }
        catch (Exception) { }
        _process?.Dispose();
        _stdinLock.Dispose();
    }
}
