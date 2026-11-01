using System.ComponentModel;
using Microsoft.UI.Xaml.Media.Imaging;

namespace WindowsAIPhotos.Models;

public sealed class PhotoCard : INotifyPropertyChanged
{
    private bool _isSelected;

    public event PropertyChangedEventHandler? PropertyChanged;

    // Mirrors the grid's selection; the item template draws the check (the grid's own frame is off).
    public bool IsSelected
    {
        get => _isSelected;
        set
        {
            if (_isSelected == value) return;
            _isSelected = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(SelectedOpacity)));
        }
    }
    public double SelectedOpacity => _isSelected ? 1 : 0;

    public required string Path { get; init; }
    public required string FileName { get; init; }
    public string Subtitle { get; init; } = "";
    // Only a URI is kept. The item template calls CreateThumbnail when a card is shown, after its
    // text (x:Phase 1), so cards that never scroll into view hold no bitmap.
    public Uri? ThumbnailUri { get; init; }
    public BitmapImage? CreateThumbnail() =>
        ThumbnailUri is null ? null : new BitmapImage(ThumbnailUri) { DecodePixelWidth = 400 };
    public bool IsVideo { get; init; }
    // A Live Photo still's motion file, played by the viewer's 实况 button.
    public string? Companion { get; init; }
    // "实况" for a Live Photo still, "视频" for a video; empty hides the corner badge.
    public string Badge { get; init; } = "";
    public double BadgeOpacity => Badge.Length == 0 ? 0 : 1;
    // Play glyph for a video Windows has no codec to draw a frame for.
    public double PlaceholderOpacity => IsVideo && ThumbnailUri is null ? 0.55 : 0;

    public static PhotoCard Create(string path, string filename, string? thumbnail, long mtimeNanoseconds,
        string? takenAt = null, string? mediaKind = null, string? companion = null)
    {
        bool video = mediaKind is "video" or "live_motion" ||
            new[] { ".mov", ".mp4", ".m4v" }.Contains(System.IO.Path.GetExtension(path), StringComparer.OrdinalIgnoreCase);
        // A video file itself is never handed to BitmapImage; only its cached poster frame.
        string? sourcePath = null;
        if (!string.IsNullOrWhiteSpace(thumbnail) && File.Exists(thumbnail))
            sourcePath = thumbnail;
        else if (!video)
            sourcePath = path;
        Uri? thumbnailUri = null;
        // Undecodable files remain in the catalog but display no thumbnail.
        if (sourcePath is not null && File.Exists(sourcePath))
            Uri.TryCreate(sourcePath, UriKind.Absolute, out thumbnailUri);

        string subtitle = "";
        try
        {
            var date = (DateTimeOffset.TryParse(takenAt, out var capture) ||
                DateTimeOffset.TryParseExact(takenAt, "yyyy:MM:dd HH:mm:ss",
                    System.Globalization.CultureInfo.InvariantCulture,
                    System.Globalization.DateTimeStyles.AssumeLocal, out capture)) ? capture
                : DateTimeOffset.FromUnixTimeMilliseconds(mtimeNanoseconds / 1_000_000);
            subtitle = date.LocalDateTime.ToString("yyyy-MM-dd");
        }
        catch (ArgumentOutOfRangeException) { }

        string badge = video ? "视频" : string.IsNullOrEmpty(companion) ? "" : "实况";
        return new PhotoCard
        {
            Path = path, FileName = filename, ThumbnailUri = thumbnailUri, IsVideo = video, Badge = badge,
            Companion = string.IsNullOrEmpty(companion) ? null : companion,
            Subtitle = badge.Length == 0 ? subtitle : badge + " · " + subtitle
        };
    }
}
