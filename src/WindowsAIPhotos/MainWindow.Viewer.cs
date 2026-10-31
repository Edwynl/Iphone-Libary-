using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Media.Core;
using Windows.Media.Playback;
using VirtualKey = Windows.System.VirtualKey;
using WindowsAIPhotos.Models;

namespace WindowsAIPhotos;

/// <summary>
/// In-app viewer: a photo, video or Live Photo over the whole window, stepping through the list
/// it was opened from (the library, or one duplicate group). HEIC, AVIF and HEVC need the
/// Windows HEIF/AV1/HEVC extensions; without them the cached thumbnail stays on screen.
/// </summary>
public sealed partial class MainWindow
{
    private IList<PhotoCard> _viewerList = Array.Empty<PhotoCard>();
    private int _viewerIndex;

    private void OpenViewer(IList<PhotoCard> list, PhotoCard card)
    {
        _viewerList = list;
        _viewerIndex = Math.Max(0, list.IndexOf(card));
        Viewer.Visibility = Visibility.Visible;
        ShowViewerItem();
        // Arrow keys go to the viewer, not to the grid underneath.
        ViewerCloseButton.Focus(FocusState.Programmatic);
    }

    private void ShowViewerItem()
    {
        StopViewerVideo();
        var card = _viewerList[_viewerIndex];
        ViewerTitle.Text = card.FileName;
        ViewerSubtitle.Text = $"{card.Subtitle}  ·  {_viewerIndex + 1:N0} / {_viewerList.Count:N0}";
        ViewerPreviousButton.Visibility = _viewerIndex > 0 ? Visibility.Visible : Visibility.Collapsed;
        ViewerNextButton.Visibility = _viewerIndex < _viewerList.Count - 1 ? Visibility.Visible : Visibility.Collapsed;
        ViewerLiveButton.Visibility = card.Companion is not null ? Visibility.Visible : Visibility.Collapsed;
        ViewerThumb.Source = card.CreateThumbnail();
        ViewerImage.Source = null;
        if (card.IsVideo)
        {
            PlayInViewer(card.Path);
            return;
        }
        // Decoded at window size: a 48 MP original would otherwise take hundreds of MB. Root, since
        // the viewer itself has no width yet when it has just been made visible.
        int width = (int)(Root.ActualWidth * (Root.XamlRoot?.RasterizationScale ?? 1));
        ViewerImage.Source = new BitmapImage(new Uri(card.Path)) { DecodePixelWidth = Math.Max(width, 1) };
    }

    private void PlayInViewer(string path)
    {
        var player = new MediaPlayer { Source = MediaSource.CreateFromUri(new Uri(path)), AutoPlay = true };
        // A Live Photo's motion plays once, then the still returns.
        if (!_viewerList[_viewerIndex].IsVideo)
            player.MediaEnded += (_, _) => DispatcherQueue.TryEnqueue(StopViewerVideo);
        ViewerVideo.SetMediaPlayer(player);
        ViewerVideo.Visibility = Visibility.Visible;
    }

    private void StopViewerVideo()
    {
        var player = ViewerVideo.MediaPlayer;
        ViewerVideo.SetMediaPlayer(null);
        ViewerVideo.Visibility = Visibility.Collapsed;
        player?.Dispose();
    }

    private void StepViewer(int delta)
    {
        if (Viewer.Visibility != Visibility.Visible) return;
        int next = _viewerIndex + delta;
        if (next < 0 || next >= _viewerList.Count) return;
        _viewerIndex = next;
        ShowViewerItem();
    }

    private void CloseViewer()
    {
        StopViewerVideo();
        ViewerThumb.Source = ViewerImage.Source = null;
        Viewer.Visibility = Visibility.Collapsed;
        _viewerList = Array.Empty<PhotoCard>();
    }

    private void ViewerLiveButton_Click(object sender, RoutedEventArgs e)
    {
        if (_viewerList[_viewerIndex].Companion is { } motion) PlayInViewer(motion);
    }

    private void ViewerOpenExternal_Click(object sender, RoutedEventArgs e) => OpenOriginal(_viewerList[_viewerIndex]);
    private void ViewerCloseButton_Click(object sender, RoutedEventArgs e) => CloseViewer();
    private void ViewerPreviousButton_Click(object sender, RoutedEventArgs e) => StepViewer(-1);
    private void ViewerNextButton_Click(object sender, RoutedEventArgs e) => StepViewer(1);

    private void ViewerKey_Invoked(KeyboardAccelerator sender, KeyboardAcceleratorInvokedEventArgs e)
    {
        switch (sender.Key)
        {
            case VirtualKey.Escape: CloseViewer(); break;
            case VirtualKey.Left: StepViewer(-1); break;
            case VirtualKey.Right: StepViewer(1); break;
        }
        e.Handled = true;
    }
}
