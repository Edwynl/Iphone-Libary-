using Microsoft.UI.Xaml;
using WindowsAIPhotos.Services;

namespace WindowsAIPhotos;

public partial class App : Application
{
    private Window? _window;

    public App()
    {
        // Recorded, not swallowed: what would have crashed the app still does.
        UnhandledException += (_, e) => AppLog.Error("Unhandled UI exception", e.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, e) => AppLog.Error("Unhandled exception", e.ExceptionObject);
        TaskScheduler.UnobservedTaskException += (_, e) => AppLog.Error("Unobserved task exception", e.Exception);
        InitializeComponent();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _window = new MainWindow();
        _window.Activate();
    }
}
