using System.Collections.ObjectModel;
using System.ComponentModel;

namespace WindowsAIPhotos.Models;

/// <summary>One group in the duplicate review: byte-identical files, or visually similar ones.</summary>
public sealed class DuplicateGroup : INotifyPropertyChanged
{
    private readonly ObservableCollection<PhotoCard> _photos = null!;  // Set by the required init.

    public event PropertyChangedEventHandler? PropertyChanged;

    public required bool Exact { get; init; }
    public required ObservableCollection<PhotoCard> Photos
    {
        get => _photos;
        init
        {
            _photos = value;
            // The title shows the count, which drops as photos are recycled.
            _photos.CollectionChanged += (_, _) => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Title)));
        }
    }
    public string Title => Exact
        ? $"完全相同 · {Photos.Count} 张（内容逐字节一致）"
        : $"相似 · {Photos.Count} 张（请人工核对）";
}
