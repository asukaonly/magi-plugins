import CoreGraphics
import Foundation

/// Seconds since the last user input event of any kind (keyboard, mouse,
/// scroll, trackpad touch). Uses CGEventSource which is a pure system call
/// with **no TCC permissions required** — unlike Accessibility-based input
/// monitoring, we never see what the user typed; only "how long ago they
/// touched the machine."
///
/// Returns nil if the underlying call fails (vanishingly rare). Callers
/// should treat nil as "no idle signal available" and skip idle-based
/// session boundaries for that capture.
public func systemIdleSeconds() -> Double? {
    // .combinedSessionState aggregates events from both this app's source
    // and the session-wide event tap, so it reflects the actual user's
    // last interaction, not just events the helper itself observed.
    //
    // .nullEvent as the event type means "any event" — kCGAnyInputEventType
    // in the C API is the same value (~0u). We pass it via the raw value
    // for portability across SDK versions.
    let anyInputEventType = CGEventType(rawValue: ~0)!
    let seconds = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: anyInputEventType)
    // CGEventSource returns a non-negative Double on success and very large
    // sentinel values (or negative on some Apple builds) when it can't read
    // the timestamp. Clamp obvious garbage to nil.
    if seconds < 0 || seconds.isNaN || seconds.isInfinite {
        return nil
    }
    return seconds
}
