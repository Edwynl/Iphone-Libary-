using Microsoft.UI.Dispatching;
using Microsoft.UI.Input;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Windows.Foundation;
using Windows.UI.Core;
using VirtualKey = Windows.System.VirtualKey;

namespace WindowsAIPhotos;

/// <summary>
/// Mouse box selection on the photo grid: drag to select (Ctrl keeps the current
/// selection), with auto-scroll at the edges; Shift+click selects a range.
/// Hit-testing uses the grid's fixed item size, so rows not yet realized by
/// virtualization are selected too.
/// </summary>
public sealed partial class MainWindow
{
    private const double DragThreshold = 6;
    private const double AutoScrollEdge = 40;
    private Point? _pressPoint;  // Where the button went down, in panel (scrolled content) coordinates.
    private uint _pressPointerId;
    private Point _lastPointer;  // Relative to PhotoGrid; re-mapped after auto-scroll.
    private bool _boxSelecting;
    private HashSet<int> _boxBaseline = new();
    private HashSet<int> _boxSelected = new();
    private int? _anchorIndex;
    private DispatcherQueueTimer? _autoScroll;

    private ItemsWrapGrid? ItemsPanel => PhotoGrid.ItemsPanelRoot as ItemsWrapGrid;

    private void InitializeBoxSelection()
    {
        // Items mark pointer events handled; listen to them anyway.
        PhotoGrid.AddHandler(UIElement.PointerPressedEvent, new PointerEventHandler(PhotoGrid_PointerPressed), true);
        PhotoGrid.AddHandler(UIElement.PointerMovedEvent, new PointerEventHandler(PhotoGrid_PointerMoved), true);
        PhotoGrid.AddHandler(UIElement.PointerReleasedEvent, new PointerEventHandler(PhotoGrid_PointerReleased), true);
        PhotoGrid.AddHandler(UIElement.PointerCaptureLostEvent, new PointerEventHandler((_, e) =>
        {
            if (ReferenceEquals(e.OriginalSource, PhotoGrid)) EndBox();
        }), true);
        _autoScroll = DispatcherQueue.CreateTimer();
        _autoScroll.Interval = TimeSpan.FromMilliseconds(30);
        _autoScroll.Tick += (_, _) => AutoScroll();
    }

    private void PhotoGrid_PointerPressed(object sender, PointerRoutedEventArgs e)
    {
        if (e.Pointer.PointerDeviceType != PointerDeviceType.Mouse ||
            !e.GetCurrentPoint(PhotoGrid).Properties.IsLeftButtonPressed ||
            ItemsPanel is not { } panel || IsInScrollBar(e.OriginalSource as DependencyObject)) return;
        _pressPoint = e.GetCurrentPoint(panel).Position;
        _pressPointerId = e.Pointer.PointerId;
    }

    private void PhotoGrid_PointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (_pressPoint is not { } start || e.Pointer.PointerId != _pressPointerId || ItemsPanel is not { } panel) return;
        _lastPointer = e.GetCurrentPoint(PhotoGrid).Position;
        if (!_boxSelecting)
        {
            var now = e.GetCurrentPoint(panel).Position;
            if (Math.Abs(now.X - start.X) < DragThreshold && Math.Abs(now.Y - start.Y) < DragThreshold) return;
            // Taking the capture from the item also cancels its click (opening the photo).
            if (!PhotoGrid.CapturePointer(e.Pointer)) return;
            BeginBox(additive: IsKeyDown(VirtualKey.Control));
        }
        UpdateBox();
    }

    private void PhotoGrid_PointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (e.Pointer.PointerId != _pressPointerId) return;
        bool dragged = _boxSelecting;
        var press = _pressPoint;
        EndBox();
        if (dragged || !_selecting || press is null || ItemsPanel is not { } panel) return;
        // A plain click in select mode: the item has already toggled itself.
        if (IndexAt(press.Value, panel) is not int index) return;
        if (IsKeyDown(VirtualKey.Shift) && _anchorIndex is int anchor)
        {
            int first = Math.Min(anchor, index);
            PhotoGrid.SelectRange(new ItemIndexRange(first, (uint)(Math.Abs(anchor - index) + 1)));
        }
        else
        {
            _anchorIndex = index;
        }
    }

    private void BeginBox(bool additive)
    {
        _boxSelecting = true;
        if (!_selecting) SetSelecting(true);
        _boxBaseline = additive
            ? PhotoGrid.SelectedRanges.SelectMany(r => Enumerable.Range(r.FirstIndex, (int)r.Length)).ToHashSet()
            : new HashSet<int>();
        if (!additive) PhotoGrid.SelectedItems.Clear();
        _boxSelected = new HashSet<int>();
        SelectionBox.Visibility = Visibility.Visible;
        _autoScroll?.Start();
    }

    private void UpdateBox()
    {
        if (ItemsPanel is not { } panel || _pressPoint is not { } start) return;
        var current = PhotoGrid.TransformToVisual(panel).TransformPoint(_lastPointer);
        var box = new Rect(start, current);
        var inside = IndexesIn(box, panel);
        foreach (int index in _boxSelected)
        {
            if (!inside.Contains(index) && !_boxBaseline.Contains(index))
                PhotoGrid.DeselectRange(new ItemIndexRange(index, 1));
        }
        foreach (int index in inside)
        {
            if (!_boxSelected.Contains(index)) PhotoGrid.SelectRange(new ItemIndexRange(index, 1));
        }
        _boxSelected = inside;
        DrawBox(panel, box);
    }

    private void EndBox()
    {
        _autoScroll?.Stop();
        _pressPoint = null;
        if (!_boxSelecting) return;
        _boxSelecting = false;
        SelectionBox.Visibility = Visibility.Collapsed;
        PhotoGrid.ReleasePointerCaptures();
    }

    private void AutoScroll()
    {
        if (!_boxSelecting || _gridScroll is null) return;
        double y = _lastPointer.Y;
        double bottomEdge = PhotoGrid.ActualHeight - AutoScrollEdge;
        double overshoot;
        if (y < AutoScrollEdge) overshoot = y - AutoScrollEdge;
        else if (y > bottomEdge) overshoot = y - bottomEdge;
        else return;
        // Faster the further past the edge; scrolling also triggers loading more photos.
        _gridScroll.ChangeView(null, _gridScroll.VerticalOffset + overshoot / 2, null, true);
        UpdateBox();
    }

    private HashSet<int> IndexesIn(Rect box, ItemsWrapGrid panel)
    {
        var indexes = new HashSet<int>();
        if (box.Right < 0 || box.Bottom < 0) return indexes;
        double width = panel.ItemWidth, height = panel.ItemHeight;
        int columns = Math.Max(1, (int)(panel.ActualWidth / width));
        int lastColumn = Math.Min(columns - 1, (int)(box.Right / width));
        for (int row = Math.Max(0, (int)(box.Top / height)); row <= (int)(box.Bottom / height); row++)
        {
            for (int column = Math.Max(0, (int)(box.Left / width)); column <= lastColumn; column++)
            {
                int index = row * columns + column;
                if (index < _photos.Count) indexes.Add(index);
            }
        }
        return indexes;
    }

    private int? IndexAt(Point point, ItemsWrapGrid panel)
    {
        int columns = Math.Max(1, (int)(panel.ActualWidth / panel.ItemWidth));
        if (point.X < 0 || point.Y < 0 || point.X >= columns * panel.ItemWidth) return null;
        int index = (int)(point.Y / panel.ItemHeight) * columns + (int)(point.X / panel.ItemWidth);
        return index < _photos.Count ? index : null;
    }

    private void DrawBox(ItemsWrapGrid panel, Rect box)
    {
        var toOverlay = panel.TransformToVisual(SelectionOverlay);
        var topLeft = toOverlay.TransformPoint(new Point(box.Left, box.Top));
        var bottomRight = toOverlay.TransformPoint(new Point(box.Right, box.Bottom));
        // The start may have scrolled out of view; draw only the visible part.
        double top = Math.Max(0, topLeft.Y);
        double bottom = Math.Min(SelectionOverlay.ActualHeight, bottomRight.Y);
        Canvas.SetLeft(SelectionBox, topLeft.X);
        Canvas.SetTop(SelectionBox, top);
        SelectionBox.Width = Math.Max(0, bottomRight.X - topLeft.X);
        SelectionBox.Height = Math.Max(0, bottom - top);
    }

    // PointerRoutedEventArgs.KeyModifiers can report None while Shift/Ctrl is held; ask the keyboard.
    private static bool IsKeyDown(VirtualKey key) =>
        InputKeyboardSource.GetKeyStateForCurrentThread(key).HasFlag(CoreVirtualKeyStates.Down);

    private bool IsInScrollBar(DependencyObject? element)
    {
        for (; element is not null && !ReferenceEquals(element, PhotoGrid); element = VisualTreeHelper.GetParent(element))
        {
            if (element is ScrollBar) return true;
        }
        return false;
    }
}
