using Microsoft.UI.Xaml.Media.Imaging;

namespace WindowsAIPhotos.Models;

public sealed class PhotoCard
{
    public required string Path { get; init; }
    public required string FileName { get; init; }
    public string Subtitle { get; init; } = "";
    public BitmapImage? Thumbnail { get; init; }

    public static PhotoCard Create(string path, string filename, string? thumbnail, long mtimeNanoseconds)
    {
        var sourcePath = !string.IsNullOrWhiteSpace(thumbnail) && File.Exists(thumbnail)
            ? thumbnail : path;
        BitmapImage? bitmap = null;
        try
        {
            if (File.Exists(sourcePath))
            {
                bitmap = new BitmapImage(new Uri(sourcePath, UriKind.Absolute));
                bitmap.DecodePixelWidth = 400;
            }
        }
        catch (Exception)
        {
            // Undecodable files remain in the catalog but display no thumbnail.
        }

        string subtitle = "";
        try
        {
            var date = DateTimeOffset.FromUnixTimeMilliseconds(mtimeNanoseconds / 1_000_000);
            subtitle = date.LocalDateTime.ToString("yyyy-MM-dd");
        }
        catch (ArgumentOutOfRangeException) { }

        return new PhotoCard { Path = path, FileName = filename, Thumbnail = bitmap, Subtitle = subtitle };
    }
}
